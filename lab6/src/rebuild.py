"""Пересборка чистого слоя из сырого — без обращения к сайтам.

raw/*.jsonl  →  6 шагов очистки (clean.py)  →  контракт (contract.py)  →
clean/dataset.parquet, clean/locations.parquet, clean/rejects.jsonl, clean/protocol.csv

Сырой слой только читается. Пересборка детерминирована: при тех же raw-файлах получается
тот же набор (контрольная сумма пишется в clean/rebuild_summary.json).

Запуск: python src/rebuild.py
"""
import hashlib
import json
import shutil
import sys
import time

import numpy as np
import pandas as pd

from clean import clean_locations, clean_weather
from common import CLEAN, RAW_FILES, now_utc, read_jsonl, write_jsonl
from contract import Location, Observation, validate_rows

RENAME = {"_source": "source", "_source_url": "source_url", "_parser_version": "parser_version"}


def to_records(df: pd.DataFrame) -> list[dict]:
    recs = df.rename(columns=RENAME).to_dict("records")
    for r in recs:
        for k, v in r.items():
            if v is pd.NaT or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
                r[k] = None
    return recs


def fingerprint(df: pd.DataFrame) -> str:
    cols = [c for c in df.columns if c not in ("collected_at",)]
    data = df[cols].sort_values(cols[0]).to_csv(index=False).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def rebuild(clean_dir=CLEAN, verbose=True):
    t0 = time.perf_counter()
    if clean_dir.exists():
        shutil.rmtree(clean_dir)          # чистый слой полностью производный — пересоздаём
    clean_dir.mkdir(parents=True)

    raw_loc = pd.DataFrame(read_jsonl(RAW_FILES["time-in.ru"]))
    raw_obs = pd.DataFrame(read_jsonl(RAW_FILES["open-meteo"]))
    if raw_loc.empty or raw_obs.empty:
        sys.exit("raw/ пуст — сначала python src/collect.py")

    loc, c1 = clean_locations(raw_loc)
    obs, c2 = clean_weather(raw_obs, loc)

    # 7. контракт
    loc_ok, loc_bad = validate_rows(to_records(loc), Location)
    obs_ok, obs_bad = validate_rows(to_records(obs), Observation)
    for bad in loc_bad:
        bad["source"] = "time-in.ru"
    for bad in obs_bad:
        bad["source"] = "open-meteo"

    protocol = c1.protocol + c2.protocol
    protocol.append({"source": "time-in.ru", "step": "7. Контракт pydantic", "rows_before": len(loc),
                     "rows_after": len(loc_ok), "changed": 0, "removed": len(loc_bad), "note": "модель Location"})
    protocol.append({"source": "open-meteo", "step": "7. Контракт pydantic", "rows_before": len(obs),
                     "rows_after": len(obs_ok), "changed": 0, "removed": len(obs_bad), "note": "модель Observation"})

    df_loc = pd.DataFrame(loc_ok)
    df_obs = pd.DataFrame(obs_ok)
    for d in (df_loc, df_obs):
        d["collected_at"] = pd.to_datetime(d["collected_at"], utc=True)
    df_obs["observation_time_utc"] = pd.to_datetime(df_obs["observation_time_utc"], utc=True)
    df_obs["precipitation_sum"] = df_obs["precipitation_sum"].astype("Float64")

    df_obs.to_parquet(clean_dir / "dataset.parquet", index=False)
    df_loc.to_parquet(clean_dir / "locations.parquet", index=False)
    rejects = c1.rejects + c2.rejects + loc_bad + obs_bad
    write_jsonl(clean_dir / "rejects.jsonl", rejects)
    pd.DataFrame(protocol).to_csv(clean_dir / "protocol.csv", index=False)

    summary = {
        "rebuilt_at": now_utc(),
        "raw_rows": {"time-in.ru": len(raw_loc), "open-meteo": len(raw_obs)},
        "raw_synthetic": {
            "time-in.ru": int(raw_loc.get("_synthetic_corruption", pd.Series(dtype=object)).notna().sum()),
            "open-meteo": int(raw_obs.get("_synthetic_corruption", pd.Series(dtype=object)).notna().sum()),
        },
        "clean_rows": {"locations": len(df_loc), "observations": len(df_obs)},
        "rejects": len(rejects),
        "fingerprint_observations": fingerprint(df_obs),
        "fingerprint_locations": fingerprint(df_loc),
        "seconds": round(time.perf_counter() - t0, 2),
    }
    (clean_dir / "rebuild_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print(pd.DataFrame(protocol).to_string(index=False))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    rebuild()
