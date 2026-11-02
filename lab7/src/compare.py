"""Таблица сравнения прогонов A/B/C и таблица экономии нагрузки по механизмам (из журналов)."""
import json
from pathlib import Path
from urllib.parse import urlparse

import yaml

from observability import read_log

ROOT = Path(__file__).resolve().parents[1]


def _req(run_id, pred=lambda e: True):
    ev = read_log(ROOT / "logs" / f"run_{run_id}.jsonl")
    return [e for e in ev if e["event"] == "request" and pred(e)]


def _sum(reqs):
    return len(reqs), sum(e["bytes"] or 0 for e in reqs)


def savings() -> list[dict]:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    hb = urlparse(cfg["sources"]["httpbin"]).netloc
    httpbin = lambda e: e["host"] == hb
    quotes = lambda e: e["host"] != hb
    author_http = lambda e: e["via"] == "http" and "/author/" in (e["url"] or "")
    browser = lambda e: e["via"] == "playwright"
    rows = [
        ("Условные запросы (ETag / Last-Modified → 304)", "C_cold", "C_warm", httpbin),
        ("Сжатие (Accept-Encoding: gzip), карточки авторов", "B", "C_cold", author_http),
        ("Инкрементальность (водяной знак: стоп на известной странице, известные авторы)", "C_cold", "C_warm", quotes),
        ("Браузер без картинок/шрифтов/медиа", "B", "C_cold", browser),
    ]
    out = []
    for name, before, after, pred in rows:
        rb, bb = _sum(_req(before, pred))
        ra, ba = _sum(_req(after, pred))
        out.append({"механизм": name, "прогоны": f"{before} → {after}", "запросов до": rb, "запросов после": ra,
                    "трафик до, КБ": round(bb / 1024, 1), "трафик после, КБ": round(ba / 1024, 1),
                    "экономия трафика, %": round((1 - ba / bb) * 100, 1) if bb else None})
    return out


METRICS = [("Время, с", "duration_s"), ("Запросов", "requests"), ("Трафик по проводу, КБ", "bytes_wire"),
           ("Доля ошибок", "error_rate"), ("Доля 429 / 403", None), ("Повторов", "retries"),
           ("Ответов 304", "not_modified"), ("Медиана латентности, мс", "latency_median_ms"),
           ("p95 латентности, мс", "latency_p95_ms"), ("Активаций предохранителя", "breaker_activations"),
           ("Собрано новых записей", "collected"), ("Подтверждено без загрузки", "known"),
           ("Статус прогона", "status")]


def fmt(key, r):
    if key is None:
        return f"{r['share_429']:.0%} / {r['share_403']:.0%}"
    v = r.get(key)
    if key == "bytes_wire":
        return f"{v / 1024:.1f}"
    if key == "error_rate":
        return f"{v:.1%}"
    return "—" if v is None else str(v)


def write(reps: dict, path: Path):
    cols = list(reps)
    md = ["# Сравнение прогонов A / B / C", "", "| Метрика | " + " | ".join(cols) + " |",
          "|---|" + "---:|" * len(cols)]
    for label, key in METRICS:
        md.append(f"| {label} | " + " | ".join(fmt(key, reps[c]) for c in cols) + " |")
    a, b, c = reps.get("A"), reps.get("B"), reps.get("C_cold")
    if a and b:
        md += ["", f"**Цена вежливости:** B дольше A на {b['duration_s'] - a['duration_s']:.1f} с "
                   f"(×{b['duration_s'] / a['duration_s']:.1f}) при одинаковом объёме."]
    if b and reps.get("C_warm"):
        w = reps["C_warm"]
        md.append(f"**Повторный прогон C:** {w['requests']} запросов и {w['bytes_wire'] / 1024:.1f} КБ "
                  f"против {b['requests']} и {b['bytes_wire'] / 1024:.1f} КБ у B "
                  f"(−{(1 - w['bytes_wire'] / b['bytes_wire']) * 100:.0f} % трафика).")
    sv = savings()
    md += ["", "## Экономия нагрузки по механизмам", "",
           "| " + " | ".join(sv[0]) + " |", "|" + "---|" * len(sv[0])]
    for r in sv:
        md.append("| " + " | ".join("—" if v is None else str(v) for v in r.values()) + " |")
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    (path.parent / "savings.json").write_text(json.dumps(sv, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(md))
