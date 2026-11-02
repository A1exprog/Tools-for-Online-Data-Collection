"""Сбор исходных данных в сырой слой (только дозапись).

Источник 1 (HTML): справочник городов России с координатами — https://time-in.ru/coordinates/russia
Источник 2 (API):  архив суточной погоды Open-Meteo — https://archive-api.open-meteo.com/v1/archive
                   (резерв при перегрузке: forecast API Open-Meteo с past_days, затем NASA POWER)

Каждая запись получает служебные поля происхождения:
_source, _source_url, _collected_at (UTC, ISO 8601), _parser_version, _status, _run_id.
Для HTML дополнительно сохраняются значения «как со страницы» (*_raw), чтобы clean
можно было пересобрать без обращения к сайту (src/rebuild.py).

Запуск из корня проекта:
    python src/collect.py                 # 25 городов × 30 дней
    python src/collect.py --cities 25 --days 30
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw"
PARSER_VERSION = "1.1"
USER_AGENT = "EducationalCrawler/1.0 (+https://example.edu; contact: student@example.edu)"

HTML_URL = "https://time-in.ru/coordinates/russia"
ROBOTS_URL = "https://time-in.ru/robots.txt"
API_URL = "https://archive-api.open-meteo.com/v1/archive"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, records: list[dict]) -> None:
    """Только дозапись: режим 'a', существующие строки не трогаются."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def robots_allows(url: str) -> bool:
    rp = RobotFileParser()
    rp.set_url(ROBOTS_URL)  # именно /robots.txt, а не главная страница
    rp.read()
    return rp.can_fetch(USER_AGENT, url)


def collect_locations(run_id: str) -> list[dict]:
    if not robots_allows(HTML_URL):
        sys.exit(f"robots.txt запрещает {HTML_URL} — сбор остановлен")
    resp = session.get(HTML_URL, timeout=30)
    collected_at = now_utc()
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one("ul.coordinates-items")
    if container is None:
        sys.exit("Не найден ul.coordinates-items — вёрстка изменилась")

    records = []
    for li in container.select("li"):
        a = li.select_one("a.coordinates-items-left")
        coords = li.select_one("div.coordinates-items-right")
        coords_raw = coords.get_text(strip=True) if coords else None
        parts = [p.strip() for p in coords_raw.split(",")] if coords_raw else []
        records.append({
            "_source": "time-in.ru",
            "_source_url": HTML_URL,
            "_collected_at": collected_at,
            "_parser_version": PARSER_VERSION,
            "_status": resp.status_code,
            "_run_id": run_id,
            # значения «как со страницы»
            "city_raw": a.get_text() if a else None,
            "city_href_raw": a.get("href") if a else None,
            "coords_raw": coords_raw,
            "latitude_raw": parts[0] if len(parts) == 2 else None,
            "longitude_raw": parts[1] if len(parts) == 2 else None,
        })
    return records


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum"


def _get_with_retry(url: str, attempts: int = 4) -> requests.Response | None:
    """429/5xx → повтор с растущей паузой (Retry-After важнее своей паузы)."""
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, timeout=60)
        except requests.RequestException as e:
            print(f"    попытка {attempt}: {type(e).__name__}: {e}")
            r = None
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return r
        if r is not None:
            print(f"    попытка {attempt}: HTTP {r.status_code}: {r.text[:120]}")
        if attempt < attempts:
            ra = r.headers.get("Retry-After") if r is not None else None
            wait = float(ra) if ra and ra.isdigit() else 5 * 2 ** (attempt - 1)   # 5, 10, 20 с
            print(f"    жду {wait:.0f} с")
            time.sleep(wait)
    return None


def collect_weather(locations: list[dict], days: int, run_id: str, pause: float) -> list[dict]:
    """Все города одним запросом: Open-Meteo принимает списки координат через запятую.
    Основной эндпоинт — архив; если он перегружен (503), берём те же суточные значения
    из forecast API с past_days (другой кластер серверов)."""
    end = datetime.now(timezone.utc).date() - timedelta(days=2)  # архив отстаёт на 1–2 дня
    start = end - timedelta(days=days - 1)
    lat = ",".join(l["latitude_raw"] for l in locations)
    lon = ",".join(l["longitude_raw"] for l in locations)
    candidates = [
        ("archive", API_URL, {"latitude": lat, "longitude": lon, "start_date": start.isoformat(),
                              "end_date": end.isoformat(), "daily": DAILY, "timezone": "UTC"}),
        ("forecast_past_days", FORECAST_URL, {"latitude": lat, "longitude": lon, "past_days": days + 3,
                                              "forecast_days": 1, "daily": DAILY, "timezone": "UTC"}),
    ]
    for name, base, params in candidates:
        url = requests.Request("GET", base, params=params).prepare().url
        print(f"  {name}: запрос по {len(locations)} городам")
        r = _get_with_retry(url, attempts=3)
        if r is None:
            print(f"  {name}: недоступен, пробую следующий эндпоинт")
            continue
        if not r.ok:
            print(f"  {name}: HTTP {r.status_code}, ответ: {r.text[:300]}")
            continue
        payload = r.json()
        payload = payload if isinstance(payload, list) else [payload]
        collected_at = now_utc()
        out = []
        for loc, item in zip(locations, payload):
            daily = item.get("daily", {})
            dates = daily.get("time", [])
            for j, d in enumerate(dates):
                if not (start.isoformat() <= d <= end.isoformat()):
                    continue                    # одинаковое окно дат для обоих эндпоинтов
                out.append({
                    "_source": "open-meteo",
                    "_source_url": url,
                    "_endpoint": name,
                    "_collected_at": collected_at,
                    "_parser_version": PARSER_VERSION,
                    "_status": r.status_code,
                    "_run_id": run_id,
                    "city_raw": loc["city_raw"],
                    "latitude_raw": loc["latitude_raw"],
                    "longitude_raw": loc["longitude_raw"],
                    "date_raw": d,
                    "temperature_2m_max_raw": daily.get("temperature_2m_max", [None] * len(dates))[j],
                    "temperature_2m_min_raw": daily.get("temperature_2m_min", [None] * len(dates))[j],
                    "precipitation_sum_raw": daily.get("precipitation_sum", [None] * len(dates))[j],
                })
        print(f"  {name}: HTTP {r.status_code}, {len(payload)} городов, {len(out)} суточных записей")
        return out
    print("  Open-Meteo недоступен целиком — резервный источник NASA POWER (по одному запросу на город)")
    return collect_power(locations, days, run_id, pause)


def collect_power(locations: list[dict], days: int, run_id: str, pause: float) -> list[dict]:
    """NASA POWER: суточные T2M_MAX, T2M_MIN, PRECTOTCORR. Значения сохраняются как есть,
    включая маркер пропуска −999 (fill value) — его обрабатывает очистка, а не сборщик."""
    end = datetime.now(timezone.utc).date() - timedelta(days=7)   # у POWER задержка данных до недели
    start = end - timedelta(days=days - 1)
    out = []
    for i, loc in enumerate(locations, 1):
        params = {"parameters": "T2M_MAX,T2M_MIN,PRECTOTCORR", "community": "AG",
                  "latitude": loc["latitude_raw"], "longitude": loc["longitude_raw"],
                  "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "format": "JSON"}
        url = requests.Request("GET", POWER_URL, params=params).prepare().url
        r = _get_with_retry(url, attempts=3)
        if r is None or not r.ok:
            print(f"  [{i}] {loc['city_raw']}: нет данных ({'HTTP %s' % r.status_code if r is not None else 'ошибка сети'})")
            continue
        collected_at = now_utc()
        par = r.json().get("properties", {}).get("parameter", {})
        tmax, tmin, pr = par.get("T2M_MAX", {}), par.get("T2M_MIN", {}), par.get("PRECTOTCORR", {})
        for key in sorted(tmax):
            out.append({
                "_source": "nasa-power",
                "_source_url": url,
                "_endpoint": "power_daily_point",
                "_collected_at": collected_at,
                "_parser_version": PARSER_VERSION,
                "_status": r.status_code,
                "_run_id": run_id,
                "city_raw": loc["city_raw"],
                "latitude_raw": loc["latitude_raw"],
                "longitude_raw": loc["longitude_raw"],
                "date_raw": f"{key[:4]}-{key[4:6]}-{key[6:]}",
                "temperature_2m_max_raw": tmax.get(key),
                "temperature_2m_min_raw": tmin.get(key),
                "precipitation_sum_raw": pr.get(key),
            })
        print(f"  [{i}/{len(locations)}] {loc['city_raw']}: {len(tmax)} дней, HTTP {r.status_code}")
        time.sleep(pause)
    return out


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # кириллица и символы в консоли Windows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", type=int, default=25)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pause", type=float, default=1.0, help="пауза между запросами к API, с")
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    print(f"run_id = {run_id}")

    locs = collect_locations(run_id)
    append_jsonl(RAW / "locations_html.jsonl", locs)
    print(f"HTML: {len(locs)} строк дописано в raw/locations_html.jsonl")

    usable = [l for l in locs if l["latitude_raw"] and l["longitude_raw"]][: args.cities]
    weather = collect_weather(usable, args.days, run_id, args.pause)
    append_jsonl(RAW / "weather_api.jsonl", weather)
    print(f"API: {len(weather)} строк дописано в raw/weather_api.jsonl")
    if not weather:
        sys.exit("API не вернул ни одной строки — см. сообщения выше (ошибка сети, блокировка или ответ API)")


if __name__ == "__main__":
    main()
