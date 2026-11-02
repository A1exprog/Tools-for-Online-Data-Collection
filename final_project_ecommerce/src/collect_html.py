"""Сбор HTML-источника: https://www.scrapingcourse.com/ecommerce/ (WooCommerce).

Диагностика (docs/diagnostics.md, src/diagnose.py) показала: карточки товаров,
цены и ссылка «следующая страница» присутствуют в исходном HTML, т.е. браузер
не нужен — достаточно requests + BeautifulSoup.

Этапы:
1. robots.txt (Disallow, Crawl-delay, строки Sitemap) — через PoliteClient;
2. sitemap товаров (если есть): lastmod как водяной знак для карточек;
3. листинг: /ecommerce/ -> /ecommerce/page/N/ по ссылке «next»
   (условные запросы, 304, возобновление по checkpoint);
4. карточки товаров: только новые/изменившиеся/устаревшие (водяной знак).
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .http_client import CircuitOpenError, FetchError, PermanentHTTPError, PoliteClient
from .observability import RunReport, log_event
from .state import Checkpoint, JsonState, RawWriter, latest_by_key, stable_hash

SOURCE = "scrapingcourse.com"
STREAM_LISTING = "scrapingcourse_listing"
STREAM_PRODUCT = "scrapingcourse_product"

# ---------------------------------------------------------------------------
# Селекторы: основной + запасные (срабатывание запасного пишется в лог)
# ---------------------------------------------------------------------------
SEL = {
    "card": ["li.product", "ul.products > li", "[data-products='item']"],
    "link": ["a.woocommerce-LoopProduct-link", "a[href*='/product/']"],
    "name": [".product-name", ".woocommerce-loop-product__title", "h2", "h3"],
    "price": ["span.price", ".price", "[data-testid='product-price']"],
    "image": ["img.product-image", "img"],
    # у простых товаров кнопка «Add to cart» (a.add_to_cart_button), у вариативных — «Select options»
    # без этого класса; общий признак обеих — атрибут data-product_id
    "cart_btn": ["a[data-product_id]", "a.add_to_cart_button"],
    "next": ["a.next.page-numbers", "a.next", "a[rel='next']", "link[rel='next']"],
    # карточка товара
    "d_title": ["h1.product_title", ".product_title", "h1"],
    "d_price": [".summary p.price", "p.price", ".summary .price"],
    "d_sku": [".product_meta .sku", "span.sku"],
    "d_cats": [".product_meta .posted_in a", ".posted_in a", "a[rel='tag'][href*='product-category']"],
    "d_tags": [".product_meta .tagged_as a", ".tagged_as a"],
    "d_stock": [".summary p.stock", "p.stock"],
    "d_desc": ["#tab-description", ".woocommerce-Tabs-panel--description", ".woocommerce-product-details__short-description"],
    "d_crumbs": ["nav.woocommerce-breadcrumb a", ".breadcrumb a"],
    "d_rating": [".woocommerce-product-rating .rating", ".woocommerce-product-rating strong.rating", ".star-rating strong.rating"],
    "d_reviews": [".woocommerce-review-link .count", ".woocommerce-product-rating .count"],
}

PRICE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


class SelectorStats:
    """Счётчик срабатываний запасных селекторов (ранний признак смены вёрстки)."""

    def __init__(self) -> None:
        self.fallbacks: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    def first(self, node: Tag, field: str, many: bool = False, optional: bool = False):
        """optional=True: отсутствие элемента — норма (например, «next» на последней странице)."""
        for i, sel in enumerate(SEL[field]):
            found = node.select(sel) if many else node.select_one(sel)
            if found:
                if i > 0:
                    self.fallbacks[f"{field}:{sel}"] = self.fallbacks.get(f"{field}:{sel}", 0) + 1
                return found
        if not optional:
            self.misses[field] = self.misses.get(field, 0) + 1
        return [] if many else None


def parse_price_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_price_block(el: Optional[Tag]) -> dict:
    """Цена WooCommerce: обычная, со скидкой (<del>/<ins>) или диапазон."""
    out: dict[str, Any] = {"price": None, "regular_price": None, "price_max": None,
                           "on_sale": False, "currency": None, "price_text": None}
    if el is None:
        return out
    out["price_text"] = " ".join(el.get_text(" ", strip=True).split())
    sym = el.select_one(".woocommerce-Price-currencySymbol")
    out["currency"] = sym.get_text(strip=True) if sym else ("$" if "$" in (out["price_text"] or "") else None)
    ins, dele = el.select_one("ins"), el.select_one("del")
    if ins is not None and dele is not None:
        out["price"] = parse_price_text(ins.get_text(" ", strip=True))
        out["regular_price"] = parse_price_text(dele.get_text(" ", strip=True))
        out["on_sale"] = True
        return out
    amounts = [parse_price_text(a.get_text(" ", strip=True)) for a in el.select(".woocommerce-Price-amount")]
    amounts = [a for a in amounts if a is not None]
    if not amounts:
        amounts = [parse_price_text(m) for m in PRICE_RE.findall(out["price_text"] or "")]
        amounts = [a for a in amounts if a is not None]
    if amounts:
        out["price"] = min(amounts)
        out["regular_price"] = min(amounts)
        if len(amounts) > 1:
            out["price_max"] = max(amounts)
    return out


def slug_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "product" in parts and parts.index("product") + 1 < len(parts):
        return parts[parts.index("product") + 1]
    return parts[-1] if parts else url


def parse_listing(html: str, page_url: str, page_no: int, stats: SelectorStats) -> tuple[list[dict], Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    cards = stats.first(soup, "card", many=True)
    for pos, card in enumerate(cards, 1):
        link = stats.first(card, "link")
        href = link.get("href") if link else None
        if not href:
            continue
        url = urljoin(page_url, href)
        name_el = stats.first(card, "name")
        img = stats.first(card, "image")
        btn = stats.first(card, "cart_btn")
        classes = card.get("class", []) or []
        post_id = next((c[5:] for c in classes if re.fullmatch(r"post-\d+", c)), None)
        stock_status = next((c for c in classes if c in ("instock", "outofstock", "onbackorder")), None)
        ptype = next((c[len("product-type-"):] for c in classes if c.startswith("product-type-")), None)
        price = parse_price_block(stats.first(card, "price"))
        items.append({
            "slug": slug_from_url(url),
            "product_id": (btn.get("data-product_id") if btn else None) or post_id,
            "sku": btn.get("data-product_sku") if btn else None,
            "name": name_el.get_text(" ", strip=True) if name_el else None,
            "url": url,
            **price,
            "stock_status": stock_status,
            "product_type": ptype,
            "category_slugs": [c[len("product_cat-"):] for c in classes if c.startswith("product_cat-")],
            "image_url": (img.get("src") or img.get("data-src")) if img else None,
            "page": page_no,
            "position": pos,
        })
    nxt = stats.first(soup, "next", optional=True)
    next_url = urljoin(page_url, nxt.get("href")) if nxt is not None and nxt.get("href") else None
    return items, next_url


def parse_detail(html: str, url: str, stats: SelectorStats) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = stats.first(soup, "d_title")
    sku = stats.first(soup, "d_sku")
    stock = stats.first(soup, "d_stock", optional=True)
    desc = stats.first(soup, "d_desc")
    rating = stats.first(soup, "d_rating", optional=True)
    rcount = stats.first(soup, "d_reviews", optional=True)
    stock_text = stock.get_text(" ", strip=True) if stock else None
    qty = re.search(r"(\d+)\s+in stock", stock_text or "", re.I)
    stock_status = None
    if stock is not None:
        cls = stock.get("class", []) or []
        stock_status = ("outofstock" if "out-of-stock" in cls else
                        "instock" if "in-stock" in cls else
                        "outofstock" if re.search(r"out of stock", stock_text or "", re.I) else "instock")
    # Вариативный товар (размер × цвет): у самого товара WooCommerce ставит класс outofstock,
    # а реальное наличие — у вариаций в JSON data-product_variations.
    variations: list = []
    form = soup.select_one("form.variations_form")
    if form is not None and form.get("data-product_variations"):
        try:
            loaded = json.loads(form["data-product_variations"])
            variations = loaded if isinstance(loaded, list) else []
        except (ValueError, TypeError):
            variations = []
    var_in_stock = sum(1 for v in variations if v.get("is_in_stock"))
    var_qty = []
    for v in variations:
        m = re.search(r"(\d+)\s+in stock", str(v.get("availability_html") or ""), re.I)
        if m:
            var_qty.append(int(m.group(1)))
        elif isinstance(v.get("max_qty"), int):
            var_qty.append(v["max_qty"])
    attributes = {}
    for sel in soup.select("table.variations select"):
        name = sel.get("name") or sel.get("id") or "attr"
        attributes[name.replace("attribute_", "")] = [o.get("value") for o in sel.select("option") if o.get("value")]
    return {
        "slug": slug_from_url(url),
        "url": url,
        "name": title.get_text(" ", strip=True) if title else None,
        **{k: v for k, v in parse_price_block(stats.first(soup, "d_price")).items()},
        "sku": sku.get_text(strip=True) if sku else None,
        "categories": [a.get_text(" ", strip=True) for a in stats.first(soup, "d_cats", many=True)],
        "tags": [a.get_text(" ", strip=True) for a in stats.first(soup, "d_tags", many=True, optional=True)],
        "breadcrumbs": [a.get_text(" ", strip=True) for a in stats.first(soup, "d_crumbs", many=True, optional=True)],
        "stock_status": stock_status,
        "stock_text": stock_text,
        "stock_qty": int(qty.group(1)) if qty else None,
        "variations_count": len(variations),
        "variations_in_stock": var_in_stock,
        "variations_stock_qty": sum(var_qty) if var_qty else None,
        "attributes": attributes,
        "rating": parse_price_text(rating.get_text(strip=True)) if rating else None,
        "review_count": int(parse_price_text(rcount.get_text(strip=True)) or 0) if rcount else None,
        "description_len": len(desc.get_text(" ", strip=True)) if desc else 0,
    }


def listing_fingerprint(p: dict) -> str:
    """Что в листинге сигнализирует об изменении карточки товара."""
    return stable_hash([p.get("name"), p.get("price"), p.get("regular_price"),
                        p.get("price_max"), p.get("stock_status")])


def _snapshot(raw_dir: Path, kind: str, name: str, html: str) -> None:
    """Сохраняем HTML как есть (gzip) — для перепарсинга и тестов на фикстурах."""
    folder = raw_dir / "html_snapshots" / kind / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]
    with gzip.open(folder / f"{safe}.html.gz", "wt", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------
def _parse_sitemap(xml_text: str) -> tuple[list[str], dict[str, Optional[str]]]:
    """Возвращает (вложенные sitemap, {url: lastmod})."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return [], {}
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    children = [e.text.strip() for e in root.findall("sm:sitemap/sm:loc", ns) if e.text]
    urls: dict[str, Optional[str]] = {}
    for u in root.findall("sm:url", ns):
        loc = u.find("sm:loc", ns)
        lm = u.find("sm:lastmod", ns)
        if loc is not None and loc.text:
            urls[loc.text.strip()] = lm.text.strip() if lm is not None and lm.text else None
    return children, urls


def read_product_sitemap(client: PoliteClient, cfg: dict, robots_sitemaps: list[str],
                         logger: logging.Logger, cache: dict, full: bool = False) -> dict[str, Optional[str]]:
    """cache — сохранённые в водяном знаке разобранные sitemap (используются при ответе 304)."""
    candidates = list(dict.fromkeys(robots_sitemaps + cfg.get("sitemap_candidates", [])))
    base_path = urlparse(cfg["base_url"]).path
    for sm_url in candidates:
        if base_path not in urlparse(sm_url).path and "product" not in sm_url:
            continue  # sitemap другой части сайта
        try:
            res = client.get(sm_url, source=SOURCE, conditional=not full)
        except FetchError as e:
            log_event(logger, "sitemap_unavailable", url=sm_url, error=str(e), kind=e.kind)
            continue
        if res.not_modified and sm_url in cache:
            log_event(logger, "sitemap_not_modified", url=sm_url, products=len(cache[sm_url]))
            return cache[sm_url]
        if res.not_modified:  # нет сохранённой копии — запросим без условия
            res = client.get(sm_url, source=SOURCE, conditional=False)
        client.remember(sm_url, res)
        children, urls = _parse_sitemap(res.text)
        products = {u: lm for u, lm in urls.items() if "/product/" in u}
        for child in [c for c in children if "product" in c][:5]:
            try:
                sub = client.get(child, source=SOURCE, conditional=False)
            except FetchError as e:
                log_event(logger, "sitemap_unavailable", url=child, error=str(e), kind=e.kind)
                continue
            _, sub_urls = _parse_sitemap(sub.text)
            products.update({u: lm for u, lm in sub_urls.items() if "/product/" in u})
        if products:
            log_event(logger, "sitemap_loaded", url=sm_url, products=len(products),
                      with_lastmod=sum(1 for v in products.values() if v))
            cache.clear()
            cache[sm_url] = products
            return products
    log_event(logger, "sitemap_not_found", note="используем только пагинацию листинга")
    return {}


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------
def collect_html(cfg: dict, client: PoliteClient, raw: RawWriter, watermark: JsonState,
                 checkpoint: Checkpoint, logger: logging.Logger, report: RunReport,
                 full: bool = False, resume: bool = False, base_url_override: Optional[str] = None) -> dict:
    src = cfg["sources"]["html"]
    base_url = base_url_override or src["base_url"]
    raw_dir = raw.raw_dir
    client.configure_domain(base_url, src["rate_limit_sec"], src["jitter_sec"])
    stats = SelectorStats()
    result = {"pages_total": 0, "pages_parsed": 0, "pages_not_modified": 0, "pages_unchanged": 0,
              "pages_resumed": 0, "listing_records": 0, "details_fetched": 0, "details_skipped": 0,
              "details_failed": 0, "sitemap_products": 0}

    pol = client.load_robots(base_url, SOURCE)
    watermark.data.setdefault("html", {}).setdefault("details", {})
    sm_cache = watermark.data["html"].setdefault("sitemap", {})
    sitemap = {} if base_url_override else read_product_sitemap(client, src, pol.sitemaps, logger,
                                                                 sm_cache, full)
    result["sitemap_products"] = len(sitemap)

    # ----------------------- 1. листинг ---------------------------------
    done_pages = checkpoint.done_set("html_pages_done") if resume else set()
    url: Optional[str] = base_url
    page_no = 0
    seen_urls: set[str] = set()
    while url and page_no < int(src["max_pages"]) and url not in seen_urls:
        page_no += 1
        seen_urls.add(url)
        result["pages_total"] += 1
        memo = client.validators.get(url, {})

        if url in done_pages and memo.get("next", "missing") != "missing":
            result["pages_resumed"] += 1
            log_event(logger, "html_page_resumed_skip", url=url, page=page_no)
            url = memo.get("next")
            continue

        res = client.get(url, source=SOURCE, conditional=not full)  # FetchError -> источник FAILED
        if res.not_modified:
            result["pages_not_modified"] += 1
            log_event(logger, "html_not_modified", url=url, page=page_no, status=304)
            next_url = memo.get("next")
        else:
            items, next_url = parse_listing(res.text, url, page_no, stats)
            if not full and client.is_same_content(url, res.content_sha256):
                result["pages_unchanged"] += 1
                log_event(logger, "html_unchanged_content", url=url, page=page_no)
            else:
                for it in items:
                    raw.append(STREAM_LISTING, SOURCE, url, it, res.status, res.content_sha256)
                result["pages_parsed"] += 1
                result["listing_records"] += len(items)
                _snapshot(raw_dir, "listing", f"page_{page_no}", res.text)
                log_event(logger, "html_page_parsed", url=url, page=page_no, cards=len(items),
                          next=next_url)
            client.remember(url, res)
        client.validators.setdefault(url, {})["next"] = next_url
        client.save_state()
        checkpoint.mark("html_pages_done", url)
        url = next_url

    if stats.fallbacks or stats.misses:
        log_event(logger, "selector_fallback", logging.WARNING, stage="listing",
                  fallbacks=stats.fallbacks, misses=stats.misses)

    # ----------------------- 2. карточки товаров -----------------------------
    if not src.get("collect_details", True):
        return result
    listing = latest_by_key(raw_dir, STREAM_LISTING, "slug")
    details_wm: dict = watermark.data["html"]["details"]
    refresh = timedelta(days=float(src.get("detail_refresh_days", 7)))
    now = datetime.now(timezone.utc)
    dstats = SelectorStats()
    for i, (slug, env) in enumerate(sorted(listing.items()), 1):
        p = env["payload"]
        fp = listing_fingerprint(p)
        lastmod = sitemap.get(p["url"])
        prev = details_wm.get(slug)
        stale = True
        if prev and not full:
            age = now - datetime.fromisoformat(prev["fetched_at"])
            stale = (prev.get("fingerprint") != fp or age > refresh
                     or prev.get("parser_version") != raw.parser_version   # новый парсер -> перечитать
                     or (lastmod is not None and lastmod != prev.get("sitemap_lastmod")))
        if not stale:
            result["details_skipped"] += 1
            continue
        try:
            res = client.get(p["url"], source=SOURCE, conditional=not full)
        except PermanentHTTPError as e:
            result["details_failed"] += 1
            report.add_incident(SOURCE, "detail_permanent_error", url=p["url"], status=e.status)
            continue
        except CircuitOpenError as e:
            report.add_incident(SOURCE, "circuit_open", url=p["url"], error=str(e))
            log_event(logger, "html_details_aborted", logging.ERROR, reason="circuit_open", done=i - 1)
            result["details_failed"] += len(listing) - i + 1
            break
        except FetchError as e:
            result["details_failed"] += 1
            report.add_incident(SOURCE, e.kind, url=p["url"], error=str(e))
            continue
        if not res.not_modified:
            detail = parse_detail(res.text, p["url"], dstats)
            detail["sitemap_lastmod"] = lastmod
            raw.append(STREAM_PRODUCT, SOURCE, p["url"], detail, res.status, res.content_sha256)
            client.remember(p["url"], res)
            if result["details_fetched"] < 3:
                _snapshot(raw_dir, "product", slug, res.text)
        result["details_fetched"] += 1
        details_wm[slug] = {"fingerprint": fp, "fetched_at": now.isoformat(timespec="seconds"),
                            "parser_version": raw.parser_version,
                            "sitemap_lastmod": lastmod}
        if i % 10 == 0:  # чекпоинт: прерванный прогон не перезапросит уже собранные карточки
            watermark.save()
            client.save_state()
            log_event(logger, "html_details_progress", done=i, total=len(listing))
    watermark.save()
    client.save_state()
    if dstats.fallbacks or dstats.misses:
        log_event(logger, "selector_fallback", logging.WARNING, stage="detail",
                  fallbacks=dstats.fallbacks, misses=dstats.misses)
    log_event(logger, "html_collect_done", **result)
    return result
