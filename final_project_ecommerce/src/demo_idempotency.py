"""Демонстрация идемпотентности: три прогона подряд, дублей нет.

    python -m src.demo_idempotency                 # 3 прогона
    python -m src.demo_idempotency --runs 5        # больше прогонов (история)

Результат печатается таблицей и сохраняется в reports/idempotency_demo.json.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from .run_pipeline import main as run_pipeline


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    db = Path(cfg["storage"]["db_path"])
    reports = Path(cfg["storage"]["reports_dir"])
    rows = []
    for i in range(1, args.runs + 1):
        code = run_pipeline(["--config", args.config, "--quiet"])
        r = json.loads((reports / "run_report_latest.json").read_text(encoding="utf-8"))
        con = sqlite3.connect(db)
        n_p, n_r, n_h = (con.execute(q).fetchone()[0] for q in (
            "SELECT COUNT(*) FROM products", "SELECT COUNT(*) FROM reviews", "SELECT COUNT(*) FROM product_history"))
        dup = con.execute("SELECT COUNT(*) FROM (SELECT source, source_id FROM products "
                          "GROUP BY 1, 2 HAVING COUNT(*) > 1)").fetchone()[0]
        con.close()
        load = r.get("load", {}).get("products", {})
        rows.append({"run": i, "run_id": r["run_id"], "exit_code": code, "duration_sec": r["duration_sec"],
                     "requests": r["requests_total"], "http_304": r["not_modified_304"],
                     "raw_appended": sum(r["records"].get("raw_appended", {}).values()),
                     "inserted": load.get("inserted"), "updated": load.get("updated"),
                     "unchanged": load.get("unchanged"), "db_products": n_p, "db_reviews": n_r,
                     "history_rows": n_h, "duplicate_keys": dup, "quality": r["quality_status"]})
    out = reports / "idempotency_demo.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    cols = ["run", "exit_code", "duration_sec", "requests", "http_304", "raw_appended", "inserted",
            "updated", "unchanged", "db_products", "db_reviews", "duplicate_keys", "quality"]
    print("\n" + " | ".join(cols))
    for row in rows:
        print(" | ".join(str(row[c]) for c in cols))
    same = len({(r["db_products"], r["db_reviews"]) for r in rows}) == 1
    print(f"\nCOUNT(*) одинаковый во всех прогонах: {same}; дублей бизнес-ключа: "
          f"{max(r['duplicate_keys'] for r in rows)}.  Сохранено: {out}")


if __name__ == "__main__":
    main()
