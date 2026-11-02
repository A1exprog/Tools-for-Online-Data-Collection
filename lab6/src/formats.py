"""Сравнение форматов хранения на своём наборе: CSV, JSON Lines, Parquet.

Замеры: размер файла, время полного чтения, время чтения 3 столбцов (медиана 7 повторов),
типы после обратного чтения: дата наблюдения и строка с ведущим нулём day_code ('0909').
Результат: clean/format_benchmark.csv

Запуск: python src/formats.py
"""
import statistics
import time
from pathlib import Path

import pandas as pd

from common import CLEAN

COLS = ["observation_id", "observation_time_utc", "temperature_2m_max"]
REPEATS = 7


def timed(fn) -> float:
    runs = []
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs) * 1000


def main():
    df = pd.read_parquet(CLEAN / "dataset.parquet")
    out = CLEAN / "format_bench"
    out.mkdir(exist_ok=True)
    paths = {"CSV": out / "dataset.csv", "JSON Lines": out / "dataset.jsonl", "Parquet": out / "dataset.parquet"}
    df.to_csv(paths["CSV"], index=False)
    df.to_json(paths["JSON Lines"], orient="records", lines=True, date_format="iso", force_ascii=False)
    df.to_parquet(paths["Parquet"], index=False, compression="snappy")

    readers = {
        "CSV": (lambda: pd.read_csv(paths["CSV"]), lambda: pd.read_csv(paths["CSV"], usecols=COLS)),
        "JSON Lines": (lambda: pd.read_json(paths["JSON Lines"], lines=True),
                       lambda: pd.read_json(paths["JSON Lines"], lines=True)[COLS]),
        "Parquet": (lambda: pd.read_parquet(paths["Parquet"]),
                    lambda: pd.read_parquet(paths["Parquet"], columns=COLS)),
    }
    lead = df.index[df["day_code"].str.startswith("0")]
    probe = int(lead[0]) if len(lead) else 0   # строка, где day_code начинается с нуля
    rows = []
    for name, (full, cols) in readers.items():
        back = full()
        rows.append({
            "format": name,
            "rows": len(back),
            "size_kb": round(Path(paths[name]).stat().st_size / 1024, 1),
            "read_full_ms": round(timed(full), 2),
            "read_3_cols_ms": round(timed(cols), 2),
            "observation_time_utc_dtype": str(back["observation_time_utc"].dtype),
            "day_code_dtype": str(back["day_code"].dtype),
            "day_code_example": repr(back["day_code"].iloc[probe]),
            "date_example": str(back["observation_time_utc"].iloc[probe]),
            "precipitation_sum_dtype": str(back["precipitation_sum"].dtype),
        })
    res = pd.DataFrame(rows)
    res.to_csv(CLEAN / "format_benchmark.csv", index=False)
    print(f"Исходный набор: {len(df)} строк; строка {probe}: day_code = {df['day_code'].iloc[probe]!r}, "
          f"дата = {df['observation_time_utc'].iloc[probe]}")
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()
