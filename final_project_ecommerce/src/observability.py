"""Наблюдаемость: структурные JSON-логи, отчёт о прогоне, оповещения.

Каждый HTTP-запрос и каждое ключевое событие пишется одной JSON-строкой
в logs/run_<run_id>.jsonl (и дублируется в консоль).
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Логи
# ---------------------------------------------------------------------------
def setup_logger(logs_dir: Path, run_id: str, quiet: bool = False) -> logging.Logger:
    """Логгер: JSON-строки в файл + консоль (UTF-8, в т.ч. в Windows)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"pipeline.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(message)s")
    fh = logging.FileHandler(logs_dir / f"run_{run_id}.jsonl", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if not quiet:
        try:  # консоль Windows по умолчанию cp1251/cp866 — переключаем на UTF-8
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        logger.addHandler(sh)
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Записать структурированное событие одной JSON-строкой."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": logging.getLevelName(level),
        "event": event,
        **fields,
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))


class Timer:
    """Контекстный менеджер для замера длительности в миллисекундах."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.elapsed_ms = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0


# ---------------------------------------------------------------------------
# Отчёт о прогоне
# ---------------------------------------------------------------------------
@dataclass
class SourceStats:
    requests: int = 0
    status_codes: dict = field(default_factory=dict)
    network_errors: int = 0
    retries: int = 0
    not_modified: int = 0
    cache_hits: int = 0
    bytes_body: int = 0
    bytes_wire: int = 0
    latencies_ms: list = field(default_factory=list)


@dataclass
class RunReport:
    run_id: str
    started_at: str
    mode: str = "full"
    finished_at: str = ""
    duration_sec: float = 0.0
    exit_code: Optional[int] = None
    sources: dict = field(default_factory=dict)          # source -> SourceStats
    source_status: dict = field(default_factory=dict)    # source -> OK / FAILED / SKIPPED
    records: dict = field(default_factory=dict)          # собранные / чистые / отброшенные
    load: dict = field(default_factory=dict)             # inserted / updated / unchanged
    quality_status: str = "UNKNOWN"
    alerts: list = field(default_factory=list)
    incidents: list = field(default_factory=list)
    resumed_from: Optional[str] = None
    robots_exceptions: list = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter, repr=False)

    # --- учёт запросов ---------------------------------------------------
    def _src(self, source: str) -> SourceStats:
        if source not in self.sources:
            self.sources[source] = SourceStats()
        return self.sources[source]

    def add_request(self, source: str, status: Optional[int], latency_ms: float,
                    bytes_body: int = 0, bytes_wire: int = 0) -> None:
        s = self._src(source)
        s.requests += 1
        if status is None:
            s.network_errors += 1
        else:
            key = str(status)
            s.status_codes[key] = s.status_codes.get(key, 0) + 1
            if status == 304:
                s.not_modified += 1
        s.latencies_ms.append(round(latency_ms, 2))
        s.bytes_body += bytes_body
        s.bytes_wire += bytes_wire

    def add_retry(self, source: str) -> None:
        self._src(source).retries += 1

    def add_cache_hit(self, source: str) -> None:
        self._src(source).cache_hits += 1

    def add_incident(self, source: str, kind: str, **details: Any) -> dict:
        inc = {"ts": utc_now(), "run_id": self.run_id, "source": source, "kind": kind, **details}
        self.incidents.append(inc)
        return inc

    # --- агрегаты ----------------------------------------------------------
    @staticmethod
    def _pct(values: list, q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return round(s[idx], 2)

    def totals(self) -> dict:
        all_lat: list = []
        codes: dict = {}
        req = errs = retries = nm = body = wire = net = hits = 0
        for s in self.sources.values():
            all_lat += s.latencies_ms
            req += s.requests
            retries += s.retries
            nm += s.not_modified
            body += s.bytes_body
            wire += s.bytes_wire
            net += s.network_errors
            hits += s.cache_hits
            for k, v in s.status_codes.items():
                codes[k] = codes.get(k, 0) + v
                if k.startswith(("4", "5")):
                    errs += v
        errs += net
        return {
            "requests_total": req,
            "status_codes": dict(sorted(codes.items())),
            "network_errors": net,
            "error_rate": round(errs / req, 4) if req else 0.0,
            "retries": retries,
            "not_modified_304": nm,
            "debug_cache_hits": hits,
            "latency_median_ms": round(statistics.median(all_lat), 2) if all_lat else 0.0,
            "latency_p95_ms": self._pct(all_lat, 0.95),
            "traffic_bytes_body": body,
            "traffic_bytes_wire": wire,
        }

    def finalize(self, exit_code: int) -> None:
        self.finished_at = utc_now()
        self.duration_sec = round(time.perf_counter() - self._t0, 2)
        self.exit_code = exit_code

    def to_dict(self) -> dict:
        per_source = {}
        for name, s in self.sources.items():
            d = asdict(s)
            lat = d.pop("latencies_ms")
            d["latency_median_ms"] = round(statistics.median(lat), 2) if lat else 0.0
            d["latency_p95_ms"] = self._pct(lat, 0.95)
            per_source[name] = d
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration_sec,
            "exit_code": self.exit_code,
            "resumed_from": self.resumed_from,
            "robots_exceptions": self.robots_exceptions,
            **self.totals(),
            "per_source": per_source,
            "source_status": self.source_status,
            "records": self.records,
            "load": self.load,
            "quality_status": self.quality_status,
            "alerts": self.alerts,
            "incidents": self.incidents,
        }


def save_json(obj: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def save_report(report: RunReport, reports_dir: Path) -> Path:
    path = save_json(report.to_dict(), reports_dir / f"run_report_{report.run_id}.json")
    save_json(report.to_dict(), reports_dir / "run_report_latest.json")  # удобно для n8n
    return path


def append_incidents(report: RunReport, reports_dir: Path) -> None:
    if not report.incidents:
        return
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / "incidents.jsonl").open("a", encoding="utf-8") as f:
        for inc in report.incidents:
            f.write(json.dumps(inc, ensure_ascii=False, default=str) + "\n")


def print_summary(report: RunReport) -> None:
    """Короткая человекочитаемая сводка в конце прогона."""
    t = report.totals()
    lines = [
        "",
        "=" * 64,
        f" ОТЧЁТ О ПРОГОНЕ {report.run_id}  (режим: {report.mode})",
        "=" * 64,
        f" Длительность: {report.duration_sec} c   Exit code: {report.exit_code}",
        f" Запросов: {t['requests_total']}  коды: {t['status_codes']}  сетевых ошибок: {t['network_errors']}",
        f" 304 Not Modified: {t['not_modified_304']}  повторов: {t['retries']}  доля ошибок: {t['error_rate']:.2%}",
        f" Латентность: медиана {t['latency_median_ms']} мс, p95 {t['latency_p95_ms']} мс",
        f" Трафик: {t['traffic_bytes_body'] / 1024:.1f} КБ (тело), {t['traffic_bytes_wire'] / 1024:.1f} КБ (по сети, gzip)",
        f" Источники: {report.source_status}",
        *([f" ⚠ Исключение из robots.txt: {e['url_prefix']} (правило {e['robots_rule']!r})"
           for e in report.robots_exceptions]),
        f" Записи: {report.records}",
        f" Загрузка в БД: {report.load}",
        f" Качество: {report.quality_status}",
        f" Оповещения: {[a['name'] + ':' + a['severity'] for a in report.alerts] or 'нет'}",
        f" Инциденты: {len(report.incidents)}",
        "=" * 64,
    ]
    print("\n".join(lines))
