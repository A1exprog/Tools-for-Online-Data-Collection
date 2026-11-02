"""Синтетическая «порча» данных для демонстрации очистки и контракта.

Сырые файлы источников НЕ меняются. Копии помечены как собранные на час раньше оригинала
(имитация предыдущего неудачного прогона), поэтому при дедупликации остаётся свежий оригинал.
 Испорченные копии ~7% записей пишутся в отдельные
файлы raw/*_synthetic.jsonl (тоже только дозапись, создаются один раз). Каждая такая
запись помечена полем _synthetic_corruption = <вид порчи>, поэтому в отчёте и в rejects
видно, какие записи синтетические.

Запуск: python src/corrupt.py   (повторный запуск ничего не дописывает)
"""
import copy
import random
from datetime import date, datetime, timedelta

from common import RAW, read_jsonl, write_jsonl

SEED = 42
SHARE = 0.07

WEATHER_KINDS = [
    "missing_tmax",          # шаг 4: пропуск обязательного поля
    "nbsp_zero_width_city",  # шаг 1 → после нормализации полный дубль (шаг 5)
    "tracking_url",          # шаг 2: utm-метки в URL → после чистки полный дубль
    "comma_decimal",         # шаг 3: «12,5» → 12.5 → дубль
    "non_numeric_tmax",      # шаг 3: «abc» → отбраковка при типизации
    "invalid_date",          # шаг 3: «2026-13-45»
    "near_duplicate_older",  # шаг 5: тот же ключ, старее и чуть другие значения
    "tmax_lt_tmin",          # шаг 6: правило tmax ≥ tmin
    "negative_precip",       # шаг 6: осадки < 0
    "temp_out_of_range",     # шаг 6: 99.9 °C
    "future_date",           # шаг 6: дата в будущем
    "unknown_location",      # шаг 6: города нет в справочнике HTML
]
RULE_KINDS = {"tmax_lt_tmin", "negative_precip", "temp_out_of_range"}
LOCATION_KINDS = [
    "missing_coords",        # шаг 4
    "nbsp_city",             # шаг 1 → дубль
    "comma_coords",          # шаг 3 → дубль
    "lat_out_of_range",      # шаг 6
    "near_duplicate_coords", # шаг 5: почти-дубль (координаты отличаются < 0.01°)
    "empty_city",            # шаг 4
]


def shift_iso(ts: str, days: int = 0, hours: int = 0) -> str:
    return (datetime.fromisoformat(ts) + timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def corrupt_weather(rec: dict, kind: str) -> dict:
    r = copy.deepcopy(rec)
    # испорченная копия имитирует более ранний неудачный прогон: собрана на час раньше
    r["_collected_at"] = shift_iso(r["_collected_at"], hours=-1)
    if kind == "missing_tmax":
        r["temperature_2m_max_raw"] = None
    elif kind == "nbsp_zero_width_city":
        r["city_raw"] = "\xa0 " + r["city_raw"] + "​ "
    elif kind == "tracking_url":
        r["_source_url"] += "&utm_source=telegram&utm_medium=post"
    elif kind == "comma_decimal":
        if r["temperature_2m_max_raw"] is not None:
            r["temperature_2m_max_raw"] = str(r["temperature_2m_max_raw"]).replace(".", ",")
    elif kind == "non_numeric_tmax":
        r["temperature_2m_max_raw"] = "abc"
    elif kind == "invalid_date":
        r["date_raw"] = "2026-13-45"
    elif kind == "near_duplicate_older":
        r["_collected_at"] = shift_iso(r["_collected_at"], -1)
        if isinstance(r["temperature_2m_max_raw"], (int, float)):
            r["temperature_2m_max_raw"] = round(r["temperature_2m_max_raw"] + 0.3, 1)
    elif kind == "tmax_lt_tmin":
        r["temperature_2m_max_raw"], r["temperature_2m_min_raw"] = (
            r["temperature_2m_min_raw"] - 1 if isinstance(r["temperature_2m_min_raw"], (int, float)) else -5,
            r["temperature_2m_max_raw"] if isinstance(r["temperature_2m_max_raw"], (int, float)) else 5,
        )
    elif kind == "negative_precip":
        r["precipitation_sum_raw"] = -1.5
    elif kind == "temp_out_of_range":
        r["temperature_2m_max_raw"] = 99.9
    elif kind == "future_date":
        r["date_raw"] = (date.today() + timedelta(days=30)).isoformat()
    elif kind == "unknown_location":
        r["city_raw"] = "Неизвестноград"
    if kind in RULE_KINDS:
        # отдельный ключ (та же дата годом раньше), чтобы запись дошла до шага 6, а не ушла в дубли
        r["date_raw"] = (date.fromisoformat(r["date_raw"]) - timedelta(days=365)).isoformat()
    r["_synthetic_corruption"] = kind
    return r


def corrupt_location(rec: dict, kind: str) -> dict:
    r = copy.deepcopy(rec)
    r["_collected_at"] = shift_iso(r["_collected_at"], hours=-1)
    if kind == "missing_coords":
        r["latitude_raw"] = r["longitude_raw"] = None
        r["coords_raw"] = ""
    elif kind == "nbsp_city":
        r["city_raw"] = r["city_raw"] + "\xa0\xa0"
    elif kind == "comma_coords":
        r["latitude_raw"] = str(r["latitude_raw"]).replace(".", ",")
    elif kind == "lat_out_of_range":
        r["latitude_raw"] = "155.75"
        r["city_raw"] = r["city_raw"] + " Новый"   # другой ключ — запись дойдёт до шага 6
    elif kind == "near_duplicate_coords":
        r["_collected_at"] = shift_iso(r["_collected_at"], -1)
        r["latitude_raw"] = f"{float(r['latitude_raw']) + 0.004:.3f}"
    elif kind == "empty_city":
        r["city_raw"] = "   "
    r["_synthetic_corruption"] = kind
    return r


def build(src, dst, kinds, fn, rng):
    if dst.exists():
        print(f"{dst.name} уже есть — не дописываю (порча делается один раз)")
        return
    rows = read_jsonl([src])
    if not rows:
        raise SystemExit(f"{src.name} пуст — сначала успешно выполните python src/collect.py")
    usable = [r for r in rows if r.get("latitude_raw") not in (None, "")]
    n = max(len(kinds), round(len(rows) * SHARE))
    picks = rng.sample(usable, n)
    out = [fn(rec, kinds[i % len(kinds)]) for i, rec in enumerate(picks)]
    write_jsonl(dst, out, mode="a")
    print(f"{dst.name}: {len(out)} испорченных копий ({len(out) / len(rows):.1%} от {len(rows)})")


def main():
    rng = random.Random(SEED)
    build(RAW / "weather_api.jsonl", RAW / "weather_api_synthetic.jsonl", WEATHER_KINDS, corrupt_weather, rng)
    # справочник: порча только для городов из погодной выборки + несколько других
    build(RAW / "locations_html.jsonl", RAW / "locations_html_synthetic.jsonl", LOCATION_KINDS, corrupt_location, rng)


if __name__ == "__main__":
    main()
