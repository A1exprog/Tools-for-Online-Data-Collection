"""Метрики качества со «светофором» и шлюз перед загрузкой в mart.

    python src/quality.py   # посчитать метрики, записать в БД и clean/quality_metrics.csv

При красном статусе check_gate() бросает QualityGateError с причиной, и mart не строится.
"""
import json
import sqlite3
import sys
from collections import Counter

import pandas as pd

from common import CLEAN, DB_PATH, RAW_FILES, now_utc, read_jsonl

ORDER = {"green": 0, "yellow": 1, "red": 2}

# (зелёный, жёлтый) пороги; higher=True — «чем больше, тем лучше»
THRESHOLDS = {
    "completeness_required": (99.0, 95.0, True),
    "completeness_optional": (95.0, 80.0, True),
    "key_uniqueness": (100.0, 99.5, True),
    "validity": (95.0, 85.0, True),
    "reject_rate": (5.0, 15.0, False),
    "business_rules_violations": (1.0, 5.0, False),
    "cross_source_consistency": (100.0, 98.0, True),
    "freshness_days": (3.0, 7.0, False),
    "coverage_city_days": (98.0, 90.0, True),
    "volume_change_pct": (10.0, 20.0, False),
}


# Нормальная задержка публикации данных у источника, сутки: свежесть оцениваем относительно неё
EXPECTED_LAG = {"open-meteo": 2, "nasa-power": 7}


class QualityGateError(RuntimeError):
    pass


def status(name: str, value, thresholds=None) -> str:
    if value is None:
        return "green"
    g, y, higher = (thresholds or THRESHOLDS)[name]
    if higher:
        return "green" if value >= g else "yellow" if value >= y else "red"
    return "green" if value <= g else "yellow" if value <= y else "red"


def previous_clean_rows(db_path=DB_PATH):
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute("SELECT clean_rows FROM quality_runs ORDER BY run_at DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return None
    return row[0] if row else None


def compute(clean_dir=CLEAN, db_path=DB_PATH) -> tuple[pd.DataFrame, dict]:
    obs = pd.read_parquet(clean_dir / "dataset.parquet")
    loc = pd.read_parquet(clean_dir / "locations.parquet")
    rejects = read_jsonl([clean_dir / "rejects.jsonl"])
    raw_obs = read_jsonl(RAW_FILES["open-meteo"])
    raw_n = len(raw_obs)
    rej_obs = [r for r in rejects if r["source"] == "open-meteo"]
    protocol = pd.read_csv(clean_dir / "protocol.csv")

    required = ["observation_id", "location_id", "observation_time_utc", "temperature_2m_max",
                "temperature_2m_min", "source", "source_url", "collected_at"]
    p6 = protocol[(protocol.source == "open-meteo") & (protocol.step == "6. Бизнес-правила")].iloc[0]
    expected_days = obs["observation_time_utc"].dt.normalize().nunique()
    expected_cells = obs["location_id"].nunique() * expected_days
    newest = obs["observation_time_utc"].max()
    prev = previous_clean_rows(db_path)
    real_raw = sum(1 for r in raw_obs if not r.get("_synthetic_corruption"))
    source = obs["source"].mode().iloc[0]
    lag = EXPECTED_LAG.get(source, 2)
    th = dict(THRESHOLDS, freshness_days=(lag + 1.0, lag + 4.0, False))

    values = {
        "completeness_required": obs[required].notna().all(axis=1).mean() * 100,
        "completeness_optional": obs["precipitation_sum"].notna().mean() * 100,
        "key_uniqueness": obs["observation_id"].nunique() / len(obs) * 100,
        "validity": len(obs) / raw_n * 100,
        "reject_rate": len(rej_obs) / raw_n * 100,
        "business_rules_violations": p6["removed"] / p6["rows_before"] * 100 if p6["rows_before"] else 0,
        "cross_source_consistency": obs["location_id"].isin(loc["location_id"]).mean() * 100,
        "freshness_days": (pd.Timestamp.now(tz="UTC") - newest).total_seconds() / 86400,
        "coverage_city_days": len(obs) / expected_cells * 100 if expected_cells else 0,
        "volume_change_pct": None if not prev else abs(len(obs) - prev) / prev * 100,
    }
    notes = {
        "completeness_required": f"{len(required)} обязательных полей",
        "completeness_optional": "precipitation_sum (NULL не заполняется)",
        "key_uniqueness": "observation_id",
        "validity": f"{len(obs)} чистых из {raw_n} сырых (реальных {real_raw}, синтетических {raw_n - real_raw})",
        "reject_rate": "; ".join(f"{k}: {v}" for k, v in Counter(r["step"] for r in rej_obs).most_common()),
        "business_rules_violations": f"{int(p6['removed'])} из {int(p6['rows_before'])} записей на шаге 6",
        "cross_source_consistency": "доля наблюдений, чья локация есть в справочнике HTML",
        "freshness_days": f"последнее наблюдение {newest.date()}; источник {source} публикует данные "
                          f"с задержкой ~{lag} сут, пороги = задержка + 1 / + 4 сут",
        "coverage_city_days": f"{len(obs)} из {expected_cells} пар «город × сутки»",
        "volume_change_pct": "первый прогон — сравнивать не с чем" if not prev else f"было {prev}, стало {len(obs)}",
    }
    rows = []
    for k, v in values.items():
        g, y, higher = th[k]
        rows.append({
            "metric": k,
            "value": None if v is None else round(float(v), 2),
            "green": f"{'≥' if higher else '≤'} {g}",
            "yellow": f"{'≥' if higher else '≤'} {y}",
            "status": status(k, v, th),
            "note": notes[k],
        })
    df = pd.DataFrame(rows)
    verdict = max(df["status"], key=ORDER.get)
    reasons = df[df["status"] == "red"]["metric"].tolist()
    meta = {"run_at": now_utc(), "raw_rows": raw_n, "clean_rows": len(obs), "verdict": verdict, "red": reasons,
            "reject_reasons": Counter(r["reason"] for r in rej_obs).most_common()}
    return df, meta


def save(df, meta, clean_dir=CLEAN, db_path=DB_PATH):
    df.to_csv(clean_dir / "quality_metrics.csv", index=False)
    (clean_dir / "quality_verdict.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO quality_runs VALUES (?, ?, ?, ?, ?)",
                     (meta["run_at"], meta["raw_rows"], meta["clean_rows"], meta["verdict"],
                      df.to_json(orient="records", force_ascii=False)))


def check_gate(meta: dict) -> None:
    """Прерывает загрузку в mart при красном светофоре."""
    if meta["verdict"] == "red":
        raise QualityGateError(f"Светофор КРАСНЫЙ: {', '.join(meta['red'])}. Загрузка в mart прервана.")
    print(f"Светофор: {meta['verdict'].upper()} — загрузка в mart разрешена.")


def main():
    df, meta = compute()
    save(df, meta)
    print(df.to_string(index=False))
    print(f"\nИтоговый вердикт: {meta['verdict'].upper()}")
    try:
        check_gate(meta)
    except QualityGateError as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
