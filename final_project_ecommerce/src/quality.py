"""Метрики качества с порогами WARN/CRITICAL, итоговый вердикт и оповещения.

Вердикт: RED — нарушен хотя бы один критический порог (mart и БД не обновляются);
YELLOW — есть предупреждения; GREEN — всё в норме.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import pandas as pd

from .observability import log_event


@dataclass
class MetricResult:
    name: str
    value: float
    direction: str
    warn: float
    critical: float
    level: str
    description: str


@dataclass
class QualityReport:
    status: str = "UNKNOWN"
    metrics: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "reasons": self.reasons,
                "metrics": [asdict(m) for m in self.metrics]}


DESCRIPTIONS = {
    "products_count": "Число товаров в чистом слое (оба источника)",
    "min_products_per_source": "Минимум товаров среди источников (изоляция отказа одного источника)",
    "reviews_count": "Число отзывов (вторая связанная сущность)",
    "reject_rate_pct": "Доля отбракованных контрактом записей, %",
    "duplicate_key_pct": "Доля повторов бизнес-ключа в сырых данных прогона, %",
    "missing_category_pct": "Доля товаров без исходной категории (полнота поля category_raw), %",
    "html_unmapped_group_pct": "Доля HTML-товаров, не сопоставленных ни с одной товарной группой, %",
    "price_outlier_pct": "Доля выбросов цены (вне Q1-3·IQR … Q3+3·IQR внутри источника), %",
    "orphan_reviews_pct": "Доля отзывов без товара (ссылочная целостность), %",
    "group_overlap_count": "Число товарных групп, представленных в обоих источниках (связь)",
    "missing_api_rating_pct": "Доля товаров API без рейтинга, %",
}


def _level(value: float, rule: dict) -> str:
    d, warn, crit = rule["direction"], float(rule["warn"]), float(rule["critical"])
    if d == "min":
        return "CRITICAL" if value < crit else "WARN" if value < warn else "OK"
    return "CRITICAL" if value > crit else "WARN" if value > warn else "OK"


def _outlier_pct(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    n_out = 0
    for _, g in df.groupby("source"):
        q1, q3 = g["price_usd"].quantile(0.25), g["price_usd"].quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
        n_out += int(((g["price_usd"] < lo) | (g["price_usd"] > hi)).sum())
    return round(n_out / len(df) * 100, 2)


def compute_metrics(products: list[dict], reviews: list[dict], rejects: list[dict],
                    protocol: dict) -> dict[str, float]:
    df = pd.DataFrame(products) if products else pd.DataFrame(
        columns=["business_key", "source", "product_group", "category_raw", "price_usd", "rating"])
    n = len(df)
    per_source = df.groupby("source").size().to_dict() if n else {}
    sources = ["scrapingcourse.com", "dummyjson.com"]
    keys = set(df["business_key"]) if n else set()
    orphans = sum(1 for r in reviews if r["product_key"] not in keys)
    api = df[df["source"] == "dummyjson.com"] if n else df
    html = df[df["source"] == "scrapingcourse.com"] if n else df
    groups_by_src = df[df["product_group"] != "other"].groupby("product_group")["source"].nunique() if n else pd.Series(dtype=int)
    total_in = n + len(reviews) + len(rejects)
    return {
        "products_count": n,
        "min_products_per_source": min(per_source.get(s, 0) for s in sources),
        "reviews_count": len(reviews),
        "reject_rate_pct": round(len(rejects) / total_in * 100, 2) if total_in else 0.0,
        "duplicate_key_pct": float(protocol.get("duplicate_key_pct", 0.0)),
        "missing_category_pct": round(df["category_raw"].isna().mean() * 100, 2) if n else 100.0,
        "html_unmapped_group_pct": round((html["product_group"] == "other").mean() * 100, 2) if len(html) else 100.0,
        "price_outlier_pct": _outlier_pct(df),
        "orphan_reviews_pct": round(orphans / len(reviews) * 100, 2) if reviews else 0.0,
        "group_overlap_count": int((groups_by_src >= 2).sum()),
        "missing_api_rating_pct": round(api["rating"].isna().mean() * 100, 2) if len(api) else 100.0,
    }


def evaluate_quality(products: list[dict], reviews: list[dict], rejects: list[dict],
                     protocol: dict, thresholds: dict, logger: logging.Logger) -> QualityReport:
    values = compute_metrics(products, reviews, rejects, protocol)
    rep = QualityReport()
    for name, rule in thresholds.items():
        value = float(values[name])
        level = _level(value, rule)
        m = MetricResult(name, value, rule["direction"], float(rule["warn"]), float(rule["critical"]),
                         level, DESCRIPTIONS.get(name, ""))
        rep.metrics.append(m)
        log_event(logger, "quality_metric", logging.INFO if level == "OK" else logging.WARNING,
                  name=name, value=value, warn=m.warn, critical=m.critical, result=level)
    crit = [m.name for m in rep.metrics if m.level == "CRITICAL"]
    warn = [m.name for m in rep.metrics if m.level == "WARN"]
    rep.status = "RED" if crit else "YELLOW" if warn else "GREEN"
    rep.reasons = crit or warn
    log_event(logger, "quality_verdict", logging.ERROR if crit else logging.INFO,
              status=rep.status, reasons=rep.reasons)
    return rep


# ---------------------------------------------------------------------------
# Оповещения
# ---------------------------------------------------------------------------
def evaluate_alerts(report: dict, prev_run: Optional[dict], cfg: dict, breaker_states: dict,
                    logger: logging.Logger) -> list[dict]:
    a = cfg["alerts"]
    alerts: list[dict] = []

    def add(name: str, severity: str, message: str, **ctx: Any) -> None:
        alerts.append({"name": name, "severity": severity, "message": message, **ctx})
        log_event(logger, "alert", logging.ERROR if severity == "critical" else logging.WARNING,
                  name=name, severity=severity, message=message, **ctx)

    # 1. отказ источника
    for src, status in report["source_status"].items():
        if status != "OK":
            add("source_failed", "critical", f"источник {src}: {status}", source=src)
    # 2. разомкнутый предохранитель
    for dom, st in breaker_states.items():
        if st != "CLOSED":
            add("circuit_open", "critical", f"предохранитель домена {dom}: {st}", domain=dom)
    # 3. доля ошибок
    er = report["error_rate"]
    if er > a["error_rate_critical"]:
        add("http_error_rate", "critical", f"доля ошибок {er:.1%}", value=er)
    elif er > a["error_rate_warn"]:
        add("http_error_rate", "warning", f"доля ошибок {er:.1%}", value=er)
    # 4. латентность
    if report["latency_p95_ms"] > a["p95_latency_ms"]:
        add("latency_p95_high", "warning", f"p95 = {report['latency_p95_ms']} мс", value=report["latency_p95_ms"])
    # 5. повторы
    if report["retries"] > a["max_retries"]:
        add("too_many_retries", "warning", f"повторов: {report['retries']}", value=report["retries"])
    # 6. объём против предыдущего прогона
    cur = (report.get("records") or {}).get("products_clean")
    if prev_run and prev_run.get("products_clean") and cur is not None:
        prev = prev_run["products_clean"]
        drop = (prev - cur) / prev * 100
        if drop > a["volume_drop_vs_previous_pct"]:
            add("volume_drop_vs_previous", "critical" if drop > 50 else "warning",
                f"товаров {cur} против {prev} в прогоне {prev_run['run_id']} (−{drop:.1f} %)",
                previous=prev, current=cur)
    # 7. красный статус качества
    if report.get("quality_status") == "RED":
        add("quality_red", "critical", "качество RED: публикация в mart заблокирована")
    return alerts
