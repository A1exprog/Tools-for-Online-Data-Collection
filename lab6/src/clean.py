"""Шесть шагов очистки с протоколом (порядок фиксирован):
1. нормализация текста  2. бизнес-ключ  3. типизация  4. пропуски  5. дубликаты  6. бизнес-правила

Каждый шаг пишет строку протокола: источник, шаг, строк до/после, изменено, удалено, примечание.
Каждая удалённая запись попадает в rejects с полем и причиной — тихих удалений нет.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from common import normalize_text, slugify, strip_tracking

# ---------- пороги бизнес-правил (климат и география России) ----------
RU_LAT = (41.0, 82.0)                 # от Дагестана до Земли Франца-Иосифа
RU_LON = [(19.0, 180.0), (-180.0, -168.0)]  # Калининград … Чукотка (за 180-м меридианом)
TEMP_RANGE = (-70.0, 50.0)            # рекорды России: −67,7 (Оймякон) … +45,4 (Калмыкия)
PRECIP_MAX = 300.0                    # мм за сутки — выше физически неправдоподобно
MAX_AMPLITUDE = 40.0                  # суточный размах tmax − tmin
MIN_DATE = pd.Timestamp("1940-01-01", tz="UTC")  # начало архива ERA5
FILL_VALUE = -999.0                   # маркер пропуска в API (NASA POWER)
COORD_TOL = 0.05                      # расхождение координат API и справочника, градусы


class Cleaner:
    def __init__(self, source: str):
        self.source = source
        self.protocol: list[dict] = []
        self.rejects: list[dict] = []

    def log(self, step, before, after, changed, note):
        self.protocol.append({
            "source": self.source, "step": step, "rows_before": before, "rows_after": after,
            "changed": int(changed), "removed": before - after, "note": note,
        })

    def reject(self, df: pd.DataFrame, mask, step: str, field: str, reason):
        """Переносит строки mask в rejects; reason — строка или Series причин."""
        for idx, row in df[mask].iterrows():
            payload = {k: (None if (isinstance(v, float) and np.isnan(v)) else v)
                       for k, v in row.items() if not k.startswith("__")}
            self.rejects.append({
                "source": self.source, "step": step, "field": field,
                "reason": reason if isinstance(reason, str) else reason.loc[idx],
                "synthetic": payload.get("_synthetic_corruption"),
                "payload": payload,
            })
        return df[~mask].copy()


def _num(v):
    """Число из текста: '12,5' → 12.5; None/'' → NaN; мусор → исключение."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    if isinstance(v, (int, float)):
        return np.nan if v <= FILL_VALUE else float(v)   # −999 у NASA POWER = «нет данных»
    s = str(v).strip().replace(",", ".")
    if s == "":
        return np.nan
    return float(s)


def _typed(df, cleaner, step, col_from, col_to, parse, field_name):
    """Применяет parse к колонке; ошибки разбора → отбраковка с причиной."""
    out, bad = [], {}
    for idx, v in df[col_from].items():
        try:
            out.append(parse(v))
        except (ValueError, TypeError) as e:
            out.append(np.nan)
            bad[idx] = f"не удалось привести {field_name}={v!r}: {e}"
    df[col_to] = out
    mask = df.index.isin(list(bad))
    return cleaner.reject(df, mask, step, field_name, pd.Series(bad, dtype=object)), len(bad)


def _utc_date(v):
    """'2026-09-09' → 2026-09-09 00:00:00+00:00 (суточные данные Open-Meteo запрошены в UTC)."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return pd.NaT
    ts = pd.Timestamp(datetime.strptime(str(v), "%Y-%m-%d"))
    return ts.tz_localize("UTC")


# ======================================================================
# Справочник локаций (HTML)
# ======================================================================
def clean_locations(raw: pd.DataFrame):
    c = Cleaner("time-in.ru")
    df = raw.copy()
    if "_synthetic_corruption" not in df:
        df["_synthetic_corruption"] = None
    text_cols = ["city_raw", "city_href_raw", "coords_raw", "latitude_raw", "longitude_raw", "_source_url"]

    # 1. нормализация текста
    n = len(df)
    before = df[text_cols].astype(str).copy()
    for col in text_cols:
        df[col] = df[col].map(normalize_text)
    changed = (df[text_cols].astype(str) != before).any(axis=1).sum()
    c.log("1. Нормализация текста", n, len(df), changed, "NFKC, \\xa0, zero-width, пробелы")

    # 2. бизнес-ключ
    n = len(df)
    df["city"] = df["city_raw"].replace("", np.nan)
    df["location_id"] = df["city"].map(lambda s: slugify(s) if isinstance(s, str) else np.nan)
    df["_source_url"] = df["_source_url"].map(strip_tracking)
    c.log("2. Бизнес-ключ", n, len(df), df["location_id"].notna().sum(),
          "location_id = транслит-slug названия города (ASCII)")

    # 3. типизация
    n = len(df)
    df, bad_lat = _typed(df, c, "3. Типизация", "latitude_raw", "latitude", _num, "latitude")
    df, bad_lon = _typed(df, c, "3. Типизация", "longitude_raw", "longitude", _num, "longitude")
    df["collected_at"] = pd.to_datetime(df["_collected_at"], utc=True)
    df["http_status"] = df["_status"].astype("Int64")
    comma = df["latitude_raw"].astype(str).str.contains(",") | df["longitude_raw"].astype(str).str.contains(",")
    c.log("3. Типизация", n, len(df), comma.sum(),
          "координаты → float (запятая → точка), время сбора → datetime UTC")

    # 4. пропуски обязательных полей → отбраковка
    n = len(df)
    for col in ["city", "latitude", "longitude"]:
        df = c.reject(df, df[col].isna(), "4. Пропуски", col, f"пустое обязательное поле {col}")
    c.log("4. Пропуски", n, len(df), 0, "city, latitude, longitude обязательны; ничего не заполняем")

    # 5. дубликаты: полные → по ключу (оставляем самую свежую) → почти-дубли
    n = len(df)
    biz = ["location_id", "city", "latitude", "longitude"]
    df = df.sort_values("collected_at", ascending=False, kind="mergesort")
    full = df.duplicated(subset=biz, keep="first")
    df = c.reject(df, full, "5. Дубликаты", "location_id", "полный дубль (те же ключ и значения)")
    by_key = df.duplicated(subset=["location_id"], keep="first")
    first = df[~by_key].set_index("location_id")[["latitude", "longitude"]]
    dist = (df["latitude"] - df["location_id"].map(first["latitude"])).abs() + \
           (df["longitude"] - df["location_id"].map(first["longitude"])).abs()
    reason = np.where(dist < 0.01, "почти-дубль: координаты отличаются < 0.01°, оставлена свежая запись",
                      "дубль по ключу: оставлена самая свежая запись")
    df = c.reject(df, by_key, "5. Дубликаты", "location_id", pd.Series(reason, index=df.index))
    c.log("5. Дубликаты", n, len(df), 0,
          f"полных {int(full.sum())}, по ключу {int(by_key.sum())} (почти-дубли — {int((by_key & (dist < 0.01)).sum())})")

    # 6. бизнес-правила
    n = len(df)
    rules = {
        "latitude": (~df["latitude"].between(*RU_LAT), f"широта вне территории России {RU_LAT}"),
        "longitude": (~(df["longitude"].between(*RU_LON[0]) | df["longitude"].between(*RU_LON[1])),
                      "долгота вне территории России"),
        "latitude,longitude": ((df["latitude"] == 0) & (df["longitude"] == 0), "координаты (0, 0) — заглушка"),
        "city": (df["city"].str.len() < 2, "название города короче 2 символов"),
        "city ": (~df["city"].str.match(r"^[А-ЯЁ][А-Яа-яЁё\- .()]+$"), "название не кириллицей с заглавной буквы"),
    }
    for field, (mask, why) in rules.items():
        df = c.reject(df, mask.reindex(df.index, fill_value=False), "6. Бизнес-правила", field.strip(), why)
    c.log("6. Бизнес-правила", n, len(df), 0, "5 правил: широта, долгота, (0,0), длина и алфавит названия")

    cols = ["location_id", "city", "latitude", "longitude", "city_href_raw", "_source", "_source_url",
            "_parser_version", "collected_at", "http_status", "_synthetic_corruption"]
    return df[cols].reset_index(drop=True), c


# ======================================================================
# Наблюдения (API)
# ======================================================================
def clean_weather(raw: pd.DataFrame, locations: pd.DataFrame):
    c = Cleaner("open-meteo")
    df = raw.copy()
    if "_synthetic_corruption" not in df:
        df["_synthetic_corruption"] = None

    # 1. нормализация текста
    n = len(df)
    text_cols = ["city_raw", "date_raw", "_source_url"]
    before = df[text_cols].astype(str).copy()
    for col in text_cols:
        df[col] = df[col].map(normalize_text)
    for col in ["temperature_2m_max_raw", "temperature_2m_min_raw", "precipitation_sum_raw"]:
        df[col] = df[col].map(lambda v: normalize_text(v) if isinstance(v, str) else v)
    changed = (df[text_cols].astype(str) != before).any(axis=1).sum()
    c.log("1. Нормализация текста", n, len(df), changed, "NFKC, \\xa0, zero-width, пробелы")

    # 2. бизнес-ключ (+ нормализация URL)
    n = len(df)
    url_before = df["_source_url"].copy()
    df["_source_url"] = df["_source_url"].map(strip_tracking)
    df["city"] = df["city_raw"]
    df["location_id"] = df["city"].map(slugify)
    df["observation_id"] = df["location_id"] + "_" + df["date_raw"].astype(str)
    c.log("2. Бизнес-ключ", n, len(df), (df["_source_url"] != url_before).sum(),
          "observation_id = location_id + дата; из URL убраны utm-метки")

    # 3. типизация
    n = len(df)
    df, _ = _typed(df, c, "3. Типизация", "date_raw", "observation_time_utc", _utc_date, "observation_time_utc")
    df, _ = _typed(df, c, "3. Типизация", "temperature_2m_max_raw", "temperature_2m_max", _num, "temperature_2m_max")
    df, _ = _typed(df, c, "3. Типизация", "temperature_2m_min_raw", "temperature_2m_min", _num, "temperature_2m_min")
    df, _ = _typed(df, c, "3. Типизация", "precipitation_sum_raw", "precipitation_sum", _num, "precipitation_sum")
    df, _ = _typed(df, c, "3. Типизация", "latitude_raw", "latitude_api", _num, "latitude")
    df, _ = _typed(df, c, "3. Типизация", "longitude_raw", "longitude_api", _num, "longitude")
    df["observation_time_utc"] = pd.to_datetime(df["observation_time_utc"], utc=True)
    df["collected_at"] = pd.to_datetime(df["_collected_at"], utc=True)
    df["http_status"] = df["_status"].astype("Int64")
    df["day_code"] = df["observation_time_utc"].dt.strftime("%m%d")   # строка с ведущим нулём: '0909'
    comma = df["temperature_2m_max_raw"].map(lambda v: isinstance(v, str) and "," in v)
    c.log("3. Типизация", n, len(df), comma.sum(),
          "числа из текста (запятая → точка), даты → datetime UTC, статус → Int64, day_code — строка")

    # 4. пропуски
    n = len(df)
    for col in ["location_id", "observation_time_utc", "temperature_2m_max", "temperature_2m_min"]:
        df = c.reject(df, df[col].isna(), "4. Пропуски", col, f"пустое обязательное поле {col}")
    c.log("4. Пропуски", n, len(df), df["precipitation_sum"].isna().sum(),
          "обязательные → отбраковка; precipitation_sum необязательное → остаётся NULL, среднее не подставляем")

    # 5. дубликаты
    n = len(df)
    biz = ["observation_id", "temperature_2m_max", "temperature_2m_min", "precipitation_sum", "_source_url"]
    df = df.sort_values("collected_at", ascending=False, kind="mergesort")
    full = df.duplicated(subset=biz, keep="first")
    df = c.reject(df, full, "5. Дубликаты", "observation_id", "полный дубль (после нормализации)")
    by_key = df.duplicated(subset=["observation_id"], keep="first")
    df = c.reject(df, by_key, "5. Дубликаты", "observation_id",
                  "почти-дубль: тот же ключ, другие значения; оставлена самая свежая запись")
    c.log("5. Дубликаты", n, len(df), 0, f"полных {int(full.sum())}, почти-дублей по ключу {int(by_key.sum())}")

    # 6. бизнес-правила
    n = len(df)
    loc = locations.set_index("location_id")
    today = pd.Timestamp.now(tz="UTC").normalize()
    known = df["location_id"].isin(loc.index)
    dlat = (df["latitude_api"] - df["location_id"].map(loc["latitude"])).abs()
    dlon = (df["longitude_api"] - df["location_id"].map(loc["longitude"])).abs()
    rules = [
        ("temperature_2m_max,temperature_2m_min", df["temperature_2m_max"] < df["temperature_2m_min"],
         "tmax < tmin"),
        ("temperature_2m_max", ~df["temperature_2m_max"].between(*TEMP_RANGE), f"tmax вне климатического диапазона {TEMP_RANGE}"),
        ("temperature_2m_min", ~df["temperature_2m_min"].between(*TEMP_RANGE), f"tmin вне климатического диапазона {TEMP_RANGE}"),
        ("temperature_2m_max,temperature_2m_min",
         (df["temperature_2m_max"] - df["temperature_2m_min"]) > MAX_AMPLITUDE, f"суточный размах > {MAX_AMPLITUDE} °C"),
        ("precipitation_sum", df["precipitation_sum"].notna() & ~df["precipitation_sum"].between(0, PRECIP_MAX),
         f"осадки вне [0; {PRECIP_MAX}] мм"),
        ("observation_time_utc", df["observation_time_utc"] > today, "дата наблюдения в будущем"),
        ("observation_time_utc", df["observation_time_utc"] < MIN_DATE, "дата раньше начала архива (1940)"),
        ("location_id", ~known, "локации нет в справочнике HTML"),
        ("latitude,longitude", known & ((dlat > COORD_TOL) | (dlon > COORD_TOL)),
         f"координаты API расходятся со справочником > {COORD_TOL}°"),
    ]
    for field, mask, why in rules:
        df = c.reject(df, mask.reindex(df.index, fill_value=False), "6. Бизнес-правила", field, why)
    c.log("6. Бизнес-правила", n, len(df), 0, f"{len(rules)} правил предметной области")

    # обогащение из справочника: каноническое название и координаты локации
    df["city"] = df["location_id"].map(loc["city"])
    df["latitude"] = df["location_id"].map(loc["latitude"])
    df["longitude"] = df["location_id"].map(loc["longitude"])
    cols = ["observation_id", "location_id", "city", "latitude", "longitude", "observation_time_utc",
            "day_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
            "_source", "_source_url", "_parser_version", "collected_at", "http_status", "_synthetic_corruption"]
    out = df[cols].sort_values(["location_id", "observation_time_utc"]).reset_index(drop=True)
    return out, c
