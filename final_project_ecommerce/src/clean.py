"""Очистка с протоколом: raw (все версии) -> clean (последняя валидная версия).

Чистый слой каждый раз пересобирается из сырого: в raw лежат все версии записей,
мы берём последнюю по бизнес-ключу и прогоняем через контракт. Поэтому прогон,
в котором источники ответили 304, даёт ровно тот же чистый слой (идемпотентность).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import ValidationError

from .collect_api import STREAM as API_STREAM
from .collect_html import STREAM_LISTING, STREAM_PRODUCT
from .contract import PRODUCT_COLUMNS, REVIEW_COLUMNS, CleanProduct, CleanReview
from .observability import log_event
from .state import iter_raw, latest_by_key, stable_hash


# ---------------------------------------------------------------------------
# Связь источников: единые товарные группы
# ---------------------------------------------------------------------------
def _match(texts: list[str], rules: dict[str, list[str]]) -> Optional[str]:
    blob = " ".join(t.lower().replace("-", " ") for t in texts if t)
    for group, words in rules.items():  # порядок правил важен
        for w in words:
            if re.search(rf"\b{re.escape(w.lower())}\b", blob):
                return group
    return None


def html_group(categories: list[str], name: str, rules: dict[str, list[str]], default: str) -> str:
    """Сначала категории карточки (надёжнее), затем название товара как запасной вариант."""
    return _match(categories, rules) or _match([name], rules) or default


def api_group(category: Optional[str], mapping: dict[str, str], default: str) -> str:
    return mapping.get((category or "").lower(), default)


# ---------------------------------------------------------------------------
class Protocol:
    """Протокол очистки: что и сколько раз сделано, сколько отброшено и почему."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.steps: Counter = Counter()
        self.rejects: list[dict] = []

    def step(self, name: str, n: int = 1) -> None:
        self.steps[name] += n

    def reject(self, stage: str, source: str, key: Any, reason: Any, record: dict) -> None:
        self.rejects.append({"run_id": self.run_id, "stage": stage, "source": source, "key": key,
                             "reason": reason, "record": record})
        self.step(f"rejected:{stage}")


def _errors(e: ValidationError) -> list[dict]:
    return [{"loc": list(err.get("loc", [])), "msg": err.get("msg"), "input": err.get("input")}
            for err in e.errors()]


def _validate_product(rec: dict, proto: Protocol) -> Optional[dict]:
    try:
        m = CleanProduct(**rec)
        return m.model_dump()
    except ValidationError as e:
        proto.reject("contract_product", rec.get("source"), rec.get("business_key"), _errors(e), rec)
        return None


# ---------------------------------------------------------------------------
# HTML -> товары
# ---------------------------------------------------------------------------
def build_html_products(raw_dir: Path, cfg: dict, proto: Protocol, parser_version: str) -> list[dict]:
    rules = cfg["linkage"]["html_keyword_rules"]
    default = cfg["linkage"]["default_group"]
    listing = latest_by_key(raw_dir, STREAM_LISTING, "slug")
    details = latest_by_key(raw_dir, STREAM_PRODUCT, "slug")
    out = []
    for slug, env in listing.items():
        p = env["payload"]
        d = (details.get(slug) or {}).get("payload", {})
        proto.step("html_listing_records")
        if d:
            proto.step("html_enriched_with_detail")
        price = d.get("price") if d.get("price") is not None else p.get("price")
        regular = d.get("regular_price") if d.get("regular_price") is not None else p.get("regular_price")
        if price is None:
            proto.reject("parse_price", "scrapingcourse.com", slug, "missing_price",
                         {"slug": slug, "price_text": p.get("price_text")})
            continue
        if regular is None:
            regular = price
        discount = round((1 - price / regular) * 100, 2) if regular and regular > price else 0.0
        categories = d.get("categories") or []
        stock_status = d.get("stock_status") or p.get("stock_status")
        in_stock = stock_status != "outofstock"
        stock_qty = d.get("stock_qty")
        if d.get("variations_count"):  # вариативный товар: наличие считаем по вариациям
            in_stock = (d.get("variations_in_stock") or 0) > 0
            stock_qty = d.get("variations_stock_qty")
            proto.step("html_stock_from_variations")
        if stock_status is None:
            proto.step("html_stock_unknown_assumed_instock")
        group = html_group(categories + (p.get("category_slugs") or []), p.get("name") or "", rules, default)
        if group == default:
            proto.step("html_group_unmapped")
        rec = {
            "business_key": f"scrapingcourse:{slug}",
            "source": "scrapingcourse.com",
            "source_id": slug,
            "title": d.get("name") or p.get("name") or "",
            "brand": None,
            "category_raw": " | ".join(categories) if categories else (", ".join(p.get("category_slugs") or []) or None),
            "product_group": group,
            "price_usd": price,
            "regular_price_usd": regular,
            "discount_pct": discount,
            "rating": d.get("rating"),
            "review_count": d.get("review_count") or 0,
            "stock_qty": stock_qty,
            "in_stock": in_stock,
            "sku": d.get("sku") or p.get("sku"),
            "url": p.get("url"),
            "collected_at": (details.get(slug) or env)["fetched_at"],
            "parser_version": parser_version,
            "extra": {
                "product_id": p.get("product_id"),
                "product_type": p.get("product_type"),
                "price_max": p.get("price_max"),
                "on_sale": bool(p.get("on_sale") or d.get("on_sale")),
                "tags": d.get("tags") or [],
                "attributes": d.get("attributes") or {},
                "variations_count": d.get("variations_count"),
                "description_len": d.get("description_len"),
                "image_url": p.get("image_url"),
                "stock_status": stock_status,                 # класс товара в WooCommerce
                "variations_in_stock": d.get("variations_in_stock"),
                "has_detail": bool(d),
            },
        }
        rec["content_hash"] = stable_hash({k: v for k, v in rec.items() if k != "collected_at"})
        v = _validate_product(rec, proto)
        if v:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# API -> товары и отзывы
# ---------------------------------------------------------------------------
def build_api_entities(raw_dir: Path, cfg: dict, proto: Protocol,
                       parser_version: str) -> tuple[list[dict], list[dict]]:
    mapping = cfg["linkage"]["api_category_map"]
    default = cfg["linkage"]["default_group"]
    latest = latest_by_key(raw_dir, API_STREAM, "id")
    products, reviews = [], []
    for pid, env in latest.items():
        p = env["payload"]
        proto.step("api_product_records")
        price = p.get("price")
        disc = p.get("discountPercentage")
        regular = round(price / (1 - disc / 100), 2) if price and disc is not None and 0 <= disc < 100 else price
        stock = p.get("stock")
        bkey = f"dummyjson:{pid}"
        revs = p.get("reviews") or []
        rec = {
            "business_key": bkey,
            "source": "dummyjson.com",
            "source_id": str(pid),
            "title": p.get("title") or "",
            "brand": p.get("brand"),
            "category_raw": p.get("category"),
            "product_group": api_group(p.get("category"), mapping, default),
            "price_usd": price,
            "regular_price_usd": regular,
            "discount_pct": disc,
            "rating": p.get("rating"),
            "review_count": len(revs),
            "stock_qty": stock,
            "in_stock": bool(stock and stock > 0) if stock is not None else p.get("availabilityStatus") != "Out of Stock",
            "sku": p.get("sku"),
            "url": f"https://dummyjson.com/products/{pid}",
            "collected_at": env["fetched_at"],
            "parser_version": parser_version,
            "extra": {
                "tags": p.get("tags") or [],
                "availability_status": p.get("availabilityStatus"),
                "minimum_order_quantity": p.get("minimumOrderQuantity"),
                "created_at": (p.get("meta") or {}).get("createdAt"),
                "updated_at": (p.get("meta") or {}).get("updatedAt"),
                "description_len": len(p.get("description") or ""),
            },
        }
        if price is None:
            proto.reject("parse_price", "dummyjson.com", bkey, "missing_price", {"id": pid})
            continue
        rec["content_hash"] = stable_hash({k: v for k, v in rec.items() if k != "collected_at"})
        v = _validate_product(rec, proto)
        if not v:
            continue  # отзывы к отброшенному товару тоже не грузим (иначе «сироты»)
        products.append(v)
        for idx, r in enumerate(revs):
            proto.step("api_review_records")
            body = {"rating": r.get("rating"), "comment": r.get("comment"), "date": r.get("date")}
            rrec = {
                # позиция отзыва в товаре: у DummyJSON бывают одинаковые отзывы (оценка+текст+дата),
                # ключ только по содержимому склеивал их в один
                "review_key": f"{bkey}:r{idx}",
                "product_key": bkey,
                "source": "dummyjson.com",
                "rating": r.get("rating"),
                "comment": r.get("comment") or "",
                "review_date": r.get("date") or "",
                "collected_at": env["fetched_at"],
                "parser_version": parser_version,
                "content_hash": stable_hash(body),
            }
            try:
                reviews.append(CleanReview(**rrec).model_dump())
            except ValidationError as e:
                proto.reject("contract_review", "dummyjson.com", rrec["review_key"], _errors(e), rrec)
    return products, reviews


# ---------------------------------------------------------------------------
def duplicate_stats(raw_dir: Path, run_ids: set[str]) -> dict:
    """Дубли бизнес-ключа внутри текущего прогона (один товар на двух страницах и т.п.)."""
    keys = [f"scrapingcourse:{e['payload'].get('slug')}" for e in iter_raw(raw_dir, STREAM_LISTING)
            if e.get("run_id") in run_ids]
    keys += [f"dummyjson:{e['payload'].get('id')}" for e in iter_raw(raw_dir, API_STREAM)
             if e.get("run_id") in run_ids]
    c = Counter(keys)
    dups = sum(v - 1 for v in c.values() if v > 1)
    return {"keys_in_run": len(keys), "duplicates_in_run": dups,
            "duplicate_key_pct": round(dups / len(keys) * 100, 2) if keys else 0.0}


def clean_all(raw_dir: Path, clean_dir: Path, cfg: dict, run_ids: set[str], run_id: str,
              logger: logging.Logger) -> dict:
    pv = cfg["project"]["parser_version"]
    proto = Protocol(run_id)
    html_products = build_html_products(raw_dir, cfg, proto, pv)
    api_products, reviews = build_api_entities(raw_dir, cfg, proto, pv)
    products = html_products + api_products

    # защита от дублей бизнес-ключа в чистом слое (должно быть 0)
    seen: set[str] = set()
    uniq = []
    for p in products:
        if p["business_key"] in seen:
            proto.reject("dedup", p["source"], p["business_key"], "duplicate_business_key", p)
            continue
        seen.add(p["business_key"])
        uniq.append(p)
    products = uniq
    proto.step("clean_products", len(products))
    proto.step("clean_reviews", len(reviews))

    clean_dir.mkdir(parents=True, exist_ok=True)
    pdf = pd.DataFrame(products, columns=PRODUCT_COLUMNS)
    pdf["extra"] = pdf["extra"].apply(lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
    pdf.to_parquet(clean_dir / "products.parquet", index=False)
    pd.DataFrame(reviews, columns=REVIEW_COLUMNS).to_parquet(clean_dir / "reviews.parquet", index=False)

    with (clean_dir / "rejects.jsonl").open("w", encoding="utf-8") as f:  # отбраковка текущего прогона
        for r in proto.rejects:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    dup = duplicate_stats(raw_dir, run_ids)
    protocol = {"run_id": run_id, "steps": dict(proto.steps), "rejected": len(proto.rejects),
                "reject_reasons": dict(Counter(r["stage"] for r in proto.rejects)), **dup}
    (clean_dir / "cleaning_protocol.json").write_text(
        json.dumps(protocol, indent=2, ensure_ascii=False), encoding="utf-8")
    log_event(logger, "clean_done", products=len(products), reviews=len(reviews),
              html_products=len(html_products), api_products=len(api_products),
              rejected=len(proto.rejects), reject_reasons=protocol["reject_reasons"])
    return {"products": products, "reviews": reviews, "rejects": proto.rejects, "protocol": protocol}
