"""Smoke-check после прогона: ловит деградацию из-за смены вёрстки.

Правила:
  * число записей упало более чем на 20% относительно эталона → FAIL;
  * доля записей с пустыми обязательными полями > 5% → FAIL.
Эталон хранится в data/quality_summary.json и обновляется только после
успешной проверки (иначе «сломанный» прогон стал бы новым эталоном).
Код выхода: 0 — OK, 1 — FAIL (удобно для cron / n8n / CI).

Запуск из корня проекта:  python quality/smoke_check.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SMOKE_DB позволяет проверить другую БД (например, копию для имитации сбоя)
DB_PATH = os.environ.get("SMOKE_DB", os.path.join(ROOT, "data", "crawl.db"))
SUMMARY_PATH = os.path.join(ROOT, "data", "quality_summary.json")

MAX_DROP = 0.20
MAX_EMPTY = 0.05
REQUIRED = ["quote", "author", "source_tag", "author_url",
            "author_born_date", "author_born_place", "author_bio"]


def get_summary():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM quotes_full").fetchone()[0]
    authors = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    empty_cond = " OR ".join(f"COALESCE(TRIM({f}), '') = ''" for f in REQUIRED)
    empty = conn.execute(f"SELECT COUNT(*) FROM quotes_full WHERE {empty_cond}").fetchone()[0]
    per_field = {
        f: conn.execute(f"SELECT COUNT(*) FROM quotes_full WHERE COALESCE(TRIM({f}), '') = ''").fetchone()[0]
        for f in REQUIRED
    }
    conn.close()
    return {
        "total": total,
        "authors": authors,
        "empty_required": empty,
        "empty_required_ratio": round(empty / total, 4) if total else 1.0,
        "empty_by_field": per_field,
    }


def main():
    cur = get_summary()
    print("Текущий прогон:", json.dumps(cur, ensure_ascii=False))

    problems = []
    if cur["total"] == 0:
        problems.append("в БД нет ни одной записи")

    if os.path.exists(SUMMARY_PATH):
        with open(SUMMARY_PATH, encoding="utf-8") as f:
            base = json.load(f)
        if base.get("total") and cur["total"] < base["total"] * (1 - MAX_DROP):
            problems.append(
                f"число записей упало более чем на {MAX_DROP:.0%}: {base['total']} → {cur['total']}"
            )
    else:
        base = None
        print("Эталона ещё нет — этот прогон станет эталоном, если пройдёт проверку.")

    if cur["empty_required_ratio"] > MAX_EMPTY:
        bad = {k: v for k, v in cur["empty_by_field"].items() if v}
        problems.append(
            f"пустые обязательные поля в {cur['empty_required_ratio']:.1%} записей (> {MAX_EMPTY:.0%}): {bad}"
        )

    if problems:
        for p in problems:
            print("FAIL:", p)
        print("Эталон не обновлён.")
        sys.exit(1)

    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    print("OK" + (f" (эталон было: {base['total']} записей)" if base else "") + " — эталон обновлён.")


if __name__ == "__main__":
    main()
