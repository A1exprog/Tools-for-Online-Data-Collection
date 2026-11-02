"""Хранилище: схема БД, идемпотентная загрузка (UPSERT), журнал прогонов, mart."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .observability import log_event

MART_VIEWS = ["v_group_comparison", "v_overlap_groups", "v_price_rating",
              "v_review_consistency", "v_source_overview"]


def init_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classify(conn: sqlite3.Connection, table: str, key: str, rows: list[dict]) -> dict:
    existing = {r[0]: r[1] for r in conn.execute(f"SELECT {key}, content_hash FROM {table}")}
    ins = sum(1 for r in rows if r[key] not in existing)
    upd = sum(1 for r in rows if r[key] in existing and existing[r[key]] != r["content_hash"])
    return {"inserted": ins, "updated": upd, "unchanged": len(rows) - ins - upd}


PRODUCTS_UPSERT = """
INSERT INTO products (
    business_key, source, source_id, title, brand, category_raw, product_group,
    price_usd, regular_price_usd, discount_pct, rating, review_count, stock_qty, in_stock,
    sku, url, extra_json, content_hash, collected_at, first_seen_at, updated_at,
    parser_version, last_run_id
) VALUES (
    :business_key, :source, :source_id, :title, :brand, :category_raw, :product_group,
    :price_usd, :regular_price_usd, :discount_pct, :rating, :review_count, :stock_qty, :in_stock,
    :sku, :url, :extra_json, :content_hash, :collected_at, :now, :now,
    :parser_version, :run_id
)
ON CONFLICT(business_key) DO UPDATE SET
    title             = excluded.title,
    brand             = excluded.brand,
    category_raw      = excluded.category_raw,
    product_group     = excluded.product_group,
    price_usd         = excluded.price_usd,
    regular_price_usd = excluded.regular_price_usd,
    discount_pct      = excluded.discount_pct,
    rating            = excluded.rating,
    review_count      = excluded.review_count,
    stock_qty         = excluded.stock_qty,
    in_stock          = excluded.in_stock,
    sku               = excluded.sku,
    url               = excluded.url,
    extra_json        = excluded.extra_json,
    content_hash      = excluded.content_hash,
    collected_at      = excluded.collected_at,
    updated_at        = excluded.updated_at,
    parser_version    = excluded.parser_version,
    last_run_id       = excluded.last_run_id
WHERE products.content_hash <> excluded.content_hash;   -- нет реальных изменений -> строку не трогаем
"""

REVIEWS_UPSERT = """
INSERT INTO reviews (
    review_key, product_key, source, rating, comment, review_date, content_hash,
    collected_at, first_seen_at, updated_at, parser_version, last_run_id
) VALUES (
    :review_key, :product_key, :source, :rating, :comment, :review_date, :content_hash,
    :collected_at, :now, :now, :parser_version, :run_id
)
ON CONFLICT(review_key) DO UPDATE SET
    rating         = excluded.rating,
    comment        = excluded.comment,
    review_date    = excluded.review_date,
    content_hash   = excluded.content_hash,
    collected_at   = excluded.collected_at,
    updated_at     = excluded.updated_at,
    parser_version = excluded.parser_version,
    last_run_id    = excluded.last_run_id
WHERE reviews.content_hash <> excluded.content_hash;
"""


def upsert_all(conn: sqlite3.Connection, products: list[dict], reviews: list[dict],
               run_id: str, logger: logging.Logger) -> dict:
    """Идемпотентная загрузка в одной транзакции: повторный прогон не создаёт дублей."""
    now = _now()
    prows = []
    for p in products:
        r = dict(p)
        r["extra_json"] = json.dumps(r.pop("extra", {}) or {}, ensure_ascii=False, sort_keys=True)
        r["in_stock"] = int(bool(r["in_stock"]))
        r["now"], r["run_id"] = now, run_id
        prows.append(r)
    rrows = [{**r, "now": now, "run_id": run_id} for r in reviews]

    stats_p = _classify(conn, "products", "business_key", prows)
    stats_r = _classify(conn, "reviews", "review_key", rrows)
    with conn:  # транзакция: при ошибке откатится целиком
        conn.executemany(PRODUCTS_UPSERT, prows)
        conn.executemany(REVIEWS_UPSERT, rrows)
        # отзывы — дочерняя сущность, целиком пересобираемая из raw: строки, которых нет в
        # чистом слое (например, ключи старой версии парсера), удаляем — синхронизация, не дубли
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _cur_reviews (k TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _cur_reviews")
        conn.executemany("INSERT OR IGNORE INTO _cur_reviews VALUES (?)", [(r["review_key"],) for r in rrows])
        cur_products = {r["product_key"] for r in rrows} | {p["business_key"] for p in prows}
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _cur_products (k TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _cur_products")
        conn.executemany("INSERT OR IGNORE INTO _cur_products VALUES (?)", [(k,) for k in cur_products])
        stale = conn.execute("""DELETE FROM reviews WHERE product_key IN (SELECT k FROM _cur_products)
                                AND review_key NOT IN (SELECT k FROM _cur_reviews)""").rowcount
    stats_r["deleted_stale"] = stale
    totals = {
        "products": stats_p,
        "reviews": stats_r,
        "db_products_total": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "db_reviews_total": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
        "db_history_rows": conn.execute("SELECT COUNT(*) FROM product_history").fetchone()[0],
    }
    log_event(logger, "upsert_done", **totals)
    return totals


# ---------------------------------------------------------------------------
# Журнал прогонов
# ---------------------------------------------------------------------------
def previous_run(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM runs WHERE products_clean IS NOT NULL ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def record_run(conn: sqlite3.Connection, report: dict) -> None:
    load = report.get("load") or {}
    rec = report.get("records") or {}
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO runs (run_id, started_at, finished_at, mode, quality_status,
                products_clean, reviews_clean, inserted, updated, unchanged, requests_total,
                error_rate, exit_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            report["run_id"], report["started_at"], report.get("finished_at"), report.get("mode"),
            report.get("quality_status"), rec.get("products_clean"), rec.get("reviews_clean"),
            (load.get("products") or {}).get("inserted"), (load.get("products") or {}).get("updated"),
            (load.get("products") or {}).get("unchanged"), report.get("requests_total"),
            report.get("error_rate"), report.get("exit_code")))


# ---------------------------------------------------------------------------
# Mart
# ---------------------------------------------------------------------------
def build_mart(conn: sqlite3.Connection, mart_dir: Path, logger: logging.Logger) -> dict[str, int]:
    """Материализует представления в mart/ (CSV для людей, Parquet для кода)."""
    mart_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, Any] = {}
    for view in MART_VIEWS:
        df = pd.read_sql_query(f"SELECT * FROM {view}", conn)
        name = view[2:]
        df.to_csv(mart_dir / f"{name}.csv", index=False, encoding="utf-8")
        df.to_parquet(mart_dir / f"{name}.parquet", index=False)
        sizes[name] = len(df)
    (mart_dir / "_built_at.txt").write_text(_now(), encoding="utf-8")
    log_event(logger, "mart_built", **sizes)
    return sizes
