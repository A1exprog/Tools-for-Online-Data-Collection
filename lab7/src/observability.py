"""Наблюдаемость: структурные JSON-логи (одна строка — одно событие), отчёт о прогоне,
пороги оповещений и итоговый статус."""
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CORE = ("ts", "run_id", "event", "host", "url", "status", "ms", "attempt", "bytes", "error")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StructLogger:
    def __init__(self, run_id: str, path: str | Path):
        self.run_id = run_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "w", encoding="utf-8")

    def log(self, event: str, **fields):
        rec = {k: None for k in CORE}
        rec.update(ts=now_iso(), run_id=self.run_id, event=event)
        rec.update(fields)
        self.f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def read_log(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def p95(values):
    if not values:
        return None
    s = sorted(values)
    k = 0.95 * (len(s) - 1)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def build_report(log_path, extra: dict | None = None) -> dict:
    """Отчёт строится из журнала, поэтому его можно пересчитать в любой момент."""
    ev = read_log(log_path)
    req = [e for e in ev if e["event"] == "request"]
    codes = Counter(str(e["status"]) if e["status"] is not None else (e.get("error_class") or "exception") for e in req)
    errors = [e for e in req if e.get("error_class") not in ("ok", "not_modified")]
    lat = [e["ms"] for e in req if e["ms"] is not None]
    n = len(req)
    ts = [datetime.fromisoformat(e["ts"]) for e in ev]
    rec = [e for e in ev if e["event"] == "records"]
    body_sizes = [e["bytes"] for e in req if e.get("error_class") == "ok" and e["bytes"]]
    report = {
        "run_id": ev[0]["run_id"] if ev else None,
        "started_at": ev[0]["ts"] if ev else None,
        "duration_s": round((max(ts) - min(ts)).total_seconds(), 2) if ts else 0,
        "requests": n,
        "status_codes": dict(sorted(codes.items())),
        "error_rate": round(len(errors) / n, 4) if n else 0.0,
        "share_429": round(sum(e["status"] == 429 for e in req) / n, 4) if n else 0.0,
        "share_403": round(sum(e["status"] == 403 for e in req) / n, 4) if n else 0.0,
        "retries": sum(e["event"] == "retry" for e in ev),
        "latency_median_ms": statistics.median(lat) if lat else None,
        "latency_p95_ms": round(p95(lat), 1) if lat else None,
        "bytes_wire": sum(e["bytes"] or 0 for e in req),
        "bytes_decoded": sum(e.get("bytes_decoded") or e["bytes"] or 0 for e in req),
        "avg_response_bytes": round(statistics.mean(body_sizes)) if body_sizes else None,
        "not_modified": sum(e["status"] == 304 for e in req),
        "breaker_activations": sum(e["event"] == "breaker_state" and e.get("new") == "open" for e in ev),
        "breaker_skipped": sum(e["event"] == "breaker_skip" for e in ev),
        "watermark_skipped": sum(e["event"] == "watermark_skip" for e in ev),
        "fallbacks": sum(e["event"] == "fallback" for e in ev),
        "detector": dict(Counter(e.get("verdict") for e in ev if e["event"] == "detector")),
        "incidents": sum(e["event"] == "incident" for e in ev),
        "collected": sum(e.get("collected", 0) for e in rec),
        "known": sum(e.get("known", 0) or 0 for e in rec),
        "discarded": sum(e.get("discarded", 0) for e in rec),
    }
    if extra:
        report.update(extra)
    return report


# Пороги: метрика, условие срабатывания, уровень, обоснование
ALERTS = [
    ("error_rate", lambda r, c: r["error_rate"] > c["error_rate_max"], "red",
     "больше {error_rate_max:.0%} запросов с ошибкой: источник деградирует или мы его перегружаем"),
    ("share_429", lambda r, c: r["share_429"] > c["share_429_max"], "yellow",
     "доля 429 выше {share_429_max:.0%}: подходим к лимиту сайта, нужно снизить скорость"),
    ("share_403", lambda r, c: r["share_403"] > c["share_403_max"], "red",
     "любой 403 — возможная блокировка, сбор домена останавливается"),
    ("latency_p95_ms", lambda r, c: (r["latency_p95_ms"] or 0) > c["latency_p95_max_ms"], "yellow",
     "p95 латентности выше {latency_p95_max_ms} мс: сервер перегружен — адаптивная задержка растёт"),
    ("breaker_activations", lambda r, c: r["breaker_activations"] > c["breaker_activations_max"], "yellow",
     "предохранитель срабатывал: один из доменов был недоступен"),
    ("collected", lambda r, c: r["collected"] + r.get("known", 0) < c["collected_min"], "red",
     "обработано меньше {collected_min} записей (новые + подтверждённые водяным знаком): "
     "тихая заглушка или поломка селекторов"),
    ("block_verdicts", lambda r, c: r["detector"].get("блокировка", 0) > 0, "red",
     "детектор вынес вердикт «блокировка»"),
    ("history", lambda r, c: (r.get("history") or {}).get("status") in ("yellow", "red"), "history",
     "объём или средний размер ответа отклонился от медианы прошлых прогонов > 20/30 %"),
]


def evaluate_alerts(report: dict, cfg: dict) -> tuple[list[dict], str]:
    fired = []
    for metric, cond, level, why in ALERTS:
        if cond(report, cfg):
            lvl = report["history"]["status"] if level == "history" else level
            fired.append({"metric": metric, "level": lvl, "value": report.get(metric, report.get("detector")),
                          "reason": why.format(**cfg)})
    status = "red" if any(a["level"] == "red" for a in fired) else "yellow" if fired else "green"
    return fired, status
