"""Протокол диагностики источников (занятие 4): нужен ли браузер?

Шаги (повторяют ручную проверку View Source -> поиск текста -> Network/XHR):
 1. View Source: скачиваем HTML без JavaScript (requests) и считаем карточки товаров;
 2. Поиск текста: ищем в «сыром» HTML названия и цены — если они есть, JS не нужен;
 3. Network/XHR: ищем признаки клиентского рендеринга (__NEXT_DATA__, data-reactroot,
    пустой контейнер товаров) и альтернативный JSON-эндпоинт (/wp-json/);
 4. Заголовки: ETag / Last-Modified / Content-Encoding / Cache-Control / robots.txt.

    python -m src.diagnose            # результат: docs/diagnostics/diagnostic_<дата>.json
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

from .robots import RobotsTxt

JS_MARKERS = ["__NEXT_DATA__", "data-reactroot", "ng-version", "window.__INITIAL_STATE__", "__NUXT__",
              "infinite-scroll", "load-more"]


def check_html(url: str, ua: str) -> dict:
    r = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("li.product")
    names = [c.select_one(".product-name, .woocommerce-loop-product__title") for c in cards]
    names = [n.get_text(strip=True) for n in names if n]
    prices = soup.select("li.product .price")
    scripts = soup.find_all("script")
    markers = [m for m in JS_MARKERS if m in r.text]
    return {
        "url": url,
        "status": r.status_code,
        "content_type": r.headers.get("Content-Type"),
        "bytes": len(r.content),
        "headers": {h: r.headers.get(h) for h in ("ETag", "Last-Modified", "Content-Encoding",
                                                  "Cache-Control", "Server", "X-Robots-Tag")},
        "step1_view_source_cards": len(cards),
        "step2_text_found": {"names_in_raw_html": len(names), "prices_in_raw_html": len(prices),
                             "sample": names[:5]},
        "step3_xhr": {"script_tags": len(scripts), "js_render_markers": markers,
                      "wp_json_link": bool(soup.select_one("link[rel='https://api.w.org/']")),
                      "next_link": bool(soup.select_one("a.next.page-numbers"))},
        "result_count_text": (soup.select_one(".woocommerce-result-count").get_text(strip=True)
                              if soup.select_one(".woocommerce-result-count") else None),
        "verdict": ("browser_not_needed" if cards and names and prices and not markers
                    else "needs_manual_check"),
    }


def check_api(url: str, ua: str) -> dict:
    r = requests.get(url, params={"limit": 1, "select": "id,title,price"},
                     headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=20)
    etag = r.headers.get("ETag")
    r304 = None
    if etag:
        r2 = requests.get(url, params={"limit": 1, "select": "id,title,price"},
                          headers={"User-Agent": ua, "If-None-Match": etag}, timeout=20)
        r304 = r2.status_code
    body = r.json() if "json" in (r.headers.get("Content-Type") or "") else {}
    return {"url": r.url, "status": r.status_code,
            "headers": {h: r.headers.get(h) for h in ("ETag", "Content-Encoding", "Cache-Control",
                                                      "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After")},
            "conditional_request_status": r304,
            "total": body.get("total"), "keys": sorted(body.keys()),
            "sample": (body.get("products") or [None])[0]}


def check_robots(base: str, ua: str) -> dict:
    """robots.txt и вердикт по RFC 9309 (с подстановочными * и $) для точки входа."""
    m = re.match(r"^(https?://[^/]+)", base)
    url = (m.group(1) if m else base) + "/robots.txt"
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=20)
    except requests.RequestException as e:
        return {"url": url, "error": str(e)}
    out = {"url": url, "status": r.status_code, "text": r.text[:2000] if r.status_code == 200 else None}
    if r.status_code == 200:
        rp = RobotsTxt(r.text)
        rule = rp.matching_rule(ua, base)
        out.update({"entry_point": base, "allowed": rp.can_fetch(ua, base),
                    "deciding_rule": (("Allow: " if rule[0] else "Disallow: ") + rule[1]) if rule else None,
                    "crawl_delay": rp.crawl_delay(ua)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="docs/diagnostics")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    ua = cfg["http"]["user_agent"]
    html_url = cfg["sources"]["html"]["base_url"]
    api_url = cfg["sources"]["api"]["base_url"]
    res = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "html_listing": check_html(html_url, ua),
           "html_robots": check_robots(html_url, ua),
           "api": check_api(api_url, ua),
           "api_robots": check_robots(api_url, ua)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"diagnostic_{datetime.now():%Y%m%d}.json"
    path.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    h = res["html_listing"]
    print(f"HTML {h['url']}: status {h['status']}, карточек в исходном HTML: {h['step1_view_source_cards']}, "
          f"названий: {h['step2_text_found']['names_in_raw_html']}, JS-маркеры: {h['step3_xhr']['js_render_markers']}")
    print(f"Вердикт: {h['verdict']}")
    rb = res["html_robots"]
    if "allowed" in rb:
        print(f"robots.txt для {rb['entry_point']}: {'разрешено' if rb['allowed'] else 'ЗАПРЕЩЕНО'}"
              f" (правило: {rb['deciding_rule']}) — см. http.robots_exceptions в config.yaml")
    print(f"API {res['api']['url']}: status {res['api']['status']}, total={res['api']['total']}, "
          f"ETag={res['api']['headers']['ETag']!r}, повтор с If-None-Match -> {res['api']['conditional_request_status']}")
    print(f"Сохранено: {path}")


if __name__ == "__main__":
    main()
