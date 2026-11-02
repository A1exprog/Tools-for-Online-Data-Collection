"""Единая точка входа пайплайна.

    python -m src.run_pipeline                         # обычный (инкрементальный) прогон
    python -m src.run_pipeline --full                  # полный пересбор без 304/водяных знаков
    python -m src.run_pipeline --simulate-failure api  # демонстрация устойчивости

Коды завершения:
    0 — успех (GREEN/YELLOW, критических оповещений нет)
    1 — данные опубликованы, но есть критические оповещения (например, отказ источника)
    2 — фатальная ошибка (нечего очищать / сбой конфигурации)
    3 — качество RED: публикация в БД и mart заблокирована
  130 — прогон прерван (Ctrl+C); следующий запуск продолжит с чекпоинта
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .clean import clean_all
from .collect_api import SOURCE as API_SOURCE, collect_api
from .collect_html import SOURCE as HTML_SOURCE, collect_html
from .http_client import FetchError, PoliteClient
from .observability import (RunReport, append_incidents, log_event, print_summary, save_json,
                            save_report, setup_logger, utc_now)
from .quality import evaluate_alerts, evaluate_quality
from .state import Checkpoint, RawWriter, load_watermark
from .storage import build_mart, init_db, previous_run, record_run, upsert_all


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Гибридный пайплайн: scrapingcourse.com (HTML) + DummyJSON (API)")
    p.add_argument("--config", default="config.yaml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--incremental", action="store_true",
                      help="(по умолчанию) условные запросы, водяные знаки, продолжение с чекпоинта")
    mode.add_argument("--full", action="store_true",
                      help="полный пересбор: без If-None-Match и водяных знаков")
    p.add_argument("--fresh", action="store_true", help="не продолжать прерванный прогон, начать заново")
    p.add_argument("--simulate-failure", choices=["html", "api"],
                   help="подменить адрес источника на demo.failure_url (httpbin 503)")
    p.add_argument("--fail-url", help="свой адрес для --simulate-failure")
    p.add_argument("--debug-cache", action="store_true", help="файловый кэш HTTP-ответов (только отладка)")
    p.add_argument("--no-details", action="store_true", help="не заходить в карточки товаров (быстрый прогон)")
    p.add_argument("--quiet", action="store_true", help="не дублировать логи в консоль")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    st = cfg["storage"]
    dirs = {k: Path(st[k]) for k in ("raw_dir", "clean_dir", "mart_dir", "logs_dir", "reports_dir", "state_dir")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    if args.no_details:
        cfg["sources"]["html"]["collect_details"] = False

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    mode = "full" if args.full else "incremental"
    if args.simulate_failure:
        mode += f"+simulate_{args.simulate_failure}_failure"
    logger = setup_logger(dirs["logs_dir"], run_id, quiet=args.quiet)
    report = RunReport(run_id=run_id, started_at=utc_now(), mode=mode)

    client = PoliteClient(cfg, logger, report, dirs["state_dir"], debug_cache=args.debug_cache)
    raw = RawWriter(dirs["raw_dir"], run_id, cfg["project"]["parser_version"])
    watermark = load_watermark(dirs["state_dir"])
    checkpoint = Checkpoint(dirs["state_dir"])
    resumed = checkpoint.begin(run_id, fresh=args.fresh or args.full)
    report.resumed_from = resumed
    log_event(logger, "pipeline_start", run_id=run_id, mode=mode, resumed_from=resumed,
              user_agent=cfg["http"]["user_agent"])

    fail_url = args.fail_url or cfg["demo"]["failure_url"]
    collect_stats = {}
    try:
        # ------------------ 1. Сбор: каждый источник изолирован -------------
        for name, func, source in (("html", collect_html, HTML_SOURCE), ("api", collect_api, API_SOURCE)):
            override = fail_url if args.simulate_failure == name else None
            try:
                collect_stats[name] = func(cfg, client, raw, watermark, checkpoint, logger, report,
                                           full=args.full, resume=bool(resumed), base_url_override=override)
                report.source_status[source] = "OK"
            except FetchError as e:
                report.source_status[source] = "FAILED"
                report.add_incident(source, e.kind, url=e.url, status=e.status, error=str(e))
                log_event(logger, "source_failed", logging.ERROR, source=source, kind=e.kind,
                          url=e.url, error=str(e))
            except Exception as e:  # неожиданная ошибка парсера не роняет второй источник
                report.source_status[source] = "FAILED"
                report.add_incident(source, "unexpected_error", error=f"{type(e).__name__}: {e}")
                log_event(logger, "source_failed", logging.ERROR, source=source, kind="unexpected_error",
                          error=f"{type(e).__name__}: {e}")
            finally:
                client.save_state()
                watermark.save()
    except KeyboardInterrupt:
        client.save_state()
        watermark.save()
        log_event(logger, "pipeline_interrupted", logging.WARNING, note="checkpoint сохранён; "
                  "повторный запуск продолжит с места остановки")
        report.add_incident("pipeline", "interrupted")
        report.finalize(130)
        save_report(report, dirs["reports_dir"])
        append_incidents(report, dirs["reports_dir"])
        return 130

    # ------------------ 2. Очистка и контракт -----------------------------
    run_ids = {run_id} | ({resumed} if resumed else set())
    cleaned = clean_all(dirs["raw_dir"], dirs["clean_dir"], cfg, run_ids, run_id, logger)
    products, reviews, rejects = cleaned["products"], cleaned["reviews"], cleaned["rejects"]
    report.records = {
        "raw_appended": dict(raw.counts),
        "collect": collect_stats,
        "products_clean": len(products),
        "products_clean_by_source": {s: sum(1 for p in products if p["source"] == s)
                                     for s in (HTML_SOURCE, API_SOURCE)},
        "reviews_clean": len(reviews),
        "rejected": len(rejects),
    }
    if not products:
        log_event(logger, "clean_empty", logging.ERROR)

    # ------------------ 3. Качество ----------------------------------------
    quality = evaluate_quality(products, reviews, rejects, cleaned["protocol"], cfg["quality"], logger)
    report.quality_status = quality.status
    save_json(quality.to_dict(), dirs["reports_dir"] / f"quality_{run_id}.json")

    # ------------------ 4. Хранилище и mart --------------------------------
    conn = init_db(Path(st["db_path"]), Path(st["schema_path"]))
    try:
        prev = previous_run(conn)
        blocked = quality.status == "RED" and cfg["pipeline"]["fail_on_red_quality"]
        if blocked:
            log_event(logger, "publish_blocked", logging.ERROR, reasons=quality.reasons,
                      note="RED: БД и mart не обновляются, данные остаются в raw/ и clean/")
            report.load = {"skipped": True, "reason": "quality RED"}
        else:
            report.load = upsert_all(conn, products, reviews, run_id, logger)
            report.load["mart"] = build_mart(conn, dirs["mart_dir"], logger)

        # ------------------ 5. Оповещения и отчёт ---------------------------
        report.alerts = evaluate_alerts(report.to_dict(), prev, cfg, client.breaker_states(), logger)
        if not products:
            code = 2
        elif blocked:
            code = 3
        elif any(a["severity"] == "critical" for a in report.alerts):
            code = 1
        else:
            code = 0
        report.finalize(code)
        record_run(conn, report.to_dict())
    finally:
        conn.close()

    save_json(report.alerts, dirs["reports_dir"] / f"alerts_{run_id}.json")
    path = save_report(report, dirs["reports_dir"])
    append_incidents(report, dirs["reports_dir"])
    checkpoint.finish("done")
    log_event(logger, "pipeline_done", exit_code=report.exit_code, quality=report.quality_status,
              duration_sec=report.duration_sec, report=str(path))
    if not args.quiet:
        print_summary(report)
    return report.exit_code or 0


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
