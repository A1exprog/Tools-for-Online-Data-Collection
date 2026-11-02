"""Сбор API-источника: https://dummyjson.com/products (REST, JSON).

* пагинация limit/skip до исчерпания поля total;
* select — запрашиваем только нужные поля (меньше трафика);
* условные запросы по ETag (304 -> страница не изменилась);
* водяной знак: meta.updatedAt + хэш содержимого по каждому товару —
  в raw дописываются только новые/изменившиеся товары;
* минимизация ПДн: reviewerName / reviewerEmail отбрасываются при сборе.
"""
from __future__ import annotations

import logging
from typing import Optional

from .http_client import FetchError, PoliteClient
from .observability import RunReport, log_event
from .state import Checkpoint, JsonState, RawWriter, stable_hash

SOURCE = "dummyjson.com"
STREAM = "dummyjson_products"


class ApiSchemaError(FetchError):
    kind = "api_schema_error"


def strip_pii(product: dict, drop_fields: list[str]) -> dict:
    """Убираем персональные данные авторов отзывов (имя, e-mail)."""
    p = dict(product)
    reviews = []
    for r in p.get("reviews") or []:
        reviews.append({k: v for k, v in r.items() if k not in drop_fields})
    p["reviews"] = reviews
    return p


def collect_api(cfg: dict, client: PoliteClient, raw: RawWriter, watermark: JsonState,
                checkpoint: Checkpoint, logger: logging.Logger, report: RunReport,
                full: bool = False, resume: bool = False, base_url_override: Optional[str] = None) -> dict:
    src = cfg["sources"]["api"]
    base_url = base_url_override or src["base_url"]
    page_size = int(src["page_size"])
    client.configure_domain(base_url, src["rate_limit_sec"], src["jitter_sec"])
    wm: dict = watermark.data.setdefault("api", {}).setdefault("products", {})
    done_pages = checkpoint.done_set("api_pages_done") if resume else set()

    result = {"pages_total": 0, "pages_parsed": 0, "pages_not_modified": 0, "pages_resumed": 0,
              "products_seen": 0, "products_new": 0, "products_changed": 0, "products_unchanged": 0,
              "reviews_seen": 0, "api_total": None}
    skip, total = 0, None
    max_updated: Optional[str] = watermark.data["api"].get("max_updated_at")

    while total is None or skip < total:
        params = {"limit": page_size, "skip": skip, "select": src["select"]}
        key = client.full_url(base_url, params)
        memo = client.validators.get(key, {})
        result["pages_total"] += 1

        if skip in done_pages and memo.get("total") is not None:
            result["pages_resumed"] += 1
            total = memo["total"]
            log_event(logger, "api_page_resumed_skip", skip=skip)
            skip += page_size
            continue

        res = client.get(base_url, source=SOURCE, params=params, conditional=not full)
        if res.not_modified:
            result["pages_not_modified"] += 1
            total = memo.get("total")
            log_event(logger, "api_not_modified", skip=skip, status=304)
            if total is None:  # нет сохранённого total — перезапросим без условия
                res = client.get(base_url, source=SOURCE, params=params, conditional=False)
        if not res.not_modified:
            try:
                data = res.json()
            except ValueError as e:
                raise ApiSchemaError(f"invalid JSON: {e}", url=key, status=res.status) from e
            products = data.get("products")
            if not isinstance(products, list) or "total" not in data:
                raise ApiSchemaError("unexpected response shape (no products/total)", url=key)
            total = int(data["total"])
            result["api_total"] = total
            result["pages_parsed"] += 1
            for prod in products:
                result["products_seen"] += 1
                clean_prod = strip_pii(prod, src.get("drop_review_fields", []))
                result["reviews_seen"] += len(clean_prod.get("reviews") or [])
                pid = str(clean_prod.get("id"))
                h = stable_hash(clean_prod)
                upd = (clean_prod.get("meta") or {}).get("updatedAt")
                prev = wm.get(pid)
                if prev and prev.get("hash") == h and not full:
                    result["products_unchanged"] += 1
                    continue
                result["products_new" if prev is None else "products_changed"] += 1
                raw.append(STREAM, SOURCE, key, clean_prod, res.status, res.content_sha256)
                wm[pid] = {"hash": h, "updated_at": upd}
                if upd and (max_updated is None or upd > max_updated):
                    max_updated = upd
            client.remember(key, res)
            log_event(logger, "api_page_parsed", skip=skip, products=len(products), total=total)
        client.validators.setdefault(key, {})["total"] = total
        client.save_state()
        watermark.data["api"]["max_updated_at"] = max_updated
        watermark.save()
        checkpoint.mark("api_pages_done", skip)
        if total is None:
            break
        skip += page_size
        if result["pages_total"] > 1000:  # предохранитель от бесконечной пагинации
            break

    log_event(logger, "api_collect_done", **result)
    return result
