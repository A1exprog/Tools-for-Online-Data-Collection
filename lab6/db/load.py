"""Загрузка чистого слоя в SQLite: UPSERT (ON CONFLICT DO UPDATE) пакетами executemany.

Строка обновляется, только если изменились значения (условие WHERE в DO UPDATE), поэтому
повторная загрузка тех же данных не трогает updated_at. Историю (SCD2) ведут триггеры из schema.sql.

    python db/load.py           # загрузить clean/ в db/warehouse.db
    python db/load.py --demo    # три прогона + проверка ограничений в db/idempotency_demo.db
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from common import CLEAN, DB_DIR, DB_PATH, read_jsonl  # noqa: E402

SCHEMA = DB_DIR / "schema.sql"
BATCH = 200

UPSERT_LOC = """
INSERT INTO locations (location_id, city, latitude, longitude, source, source_url, parser_version,
                       collected_at, updated_at)
VALUES (:location_id, :city, :latitude, :longitude, :source, :source_url, :parser_version,
        :collected_at, :run_at)
ON CONFLICT (location_id) DO UPDATE SET
    city = excluded.city, latitude = excluded.latitude, longitude = excluded.longitude,
    source_url = excluded.source_url, parser_version = excluded.parser_version,
    collected_at = excluded.collected_at, updated_at = excluded.updated_at
WHERE locations.city IS NOT excluded.city
   OR locations.latitude IS NOT excluded.latitude
   OR locations.longitude IS NOT excluded.longitude
"""

UPSERT_OBS = """
INSERT INTO observations (observation_id, location_id, observation_time_utc, day_code,
                          temperature_2m_max, temperature_2m_min, precipitation_sum,
                          source, source_url, parser_version, collected_at, updated_at)
VALUES (:observation_id, :location_id, :observation_time_utc, :day_code,
        :temperature_2m_max, :temperature_2m_min, :precipitation_sum,
        :source, :source_url, :parser_version, :collected_at, :run_at)
ON CONFLICT (observation_id) DO UPDATE SET
    temperature_2m_max = excluded.temperature_2m_max,
    temperature_2m_min = excluded.temperature_2m_min,
    precipitation_sum  = excluded.precipitation_sum,
    source_url = excluded.source_url, parser_version = excluded.parser_version,
    collected_at = excluded.collected_at, updated_at = excluded.updated_at
WHERE observations.temperature_2m_max IS NOT excluded.temperature_2m_max
   OR observations.temperature_2m_min IS NOT excluded.temperature_2m_min
   OR observations.precipitation_sum  IS NOT excluded.precipitation_sum
"""


def now_utc_us() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def iso(v):
    return None if pd.isna(v) else pd.Timestamp(v).tz_convert("UTC").isoformat()


def prepare(loc: pd.DataFrame, obs: pd.DataFrame):
    loc = loc.copy()
    obs = obs.copy()
    loc["collected_at"] = loc["collected_at"].map(iso)
    obs["collected_at"] = obs["collected_at"].map(iso)
    obs["observation_time_utc"] = obs["observation_time_utc"].map(iso)
    obs["precipitation_sum"] = obs["precipitation_sum"].astype(object).where(obs["precipitation_sum"].notna(), None)
    return loc.to_dict("records"), obs.to_dict("records")


def batched(rows, n=BATCH):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def upsert(conn, loc_rows, obs_rows, label: str) -> dict:
    run_at = now_utc_us()
    before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    with conn:  # одна транзакция на прогон
        for chunk in batched(loc_rows):
            conn.executemany(UPSERT_LOC, [{**r, "run_at": run_at} for r in chunk])
        for chunk in batched(obs_rows):
            conn.executemany(UPSERT_OBS, [{**r, "run_at": run_at} for r in chunk])
    after = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    touched = conn.execute("SELECT COUNT(*) FROM observations WHERE updated_at = ?", (run_at,)).fetchone()[0]
    inserted = after - before
    res = {
        "run_at": run_at, "label": label, "rows_in": len(obs_rows),
        "inserted": inserted, "updated": touched - inserted,
        "unchanged": len(obs_rows) - touched, "rows_total": after,
        "history_versions": conn.execute("SELECT COUNT(*) FROM observations_history").fetchone()[0],
    }
    with conn:
        conn.execute("INSERT INTO load_runs VALUES (:run_at, :label, :rows_in, :inserted, :updated, "
                     ":unchanged, :rows_total, :history_versions)", res)
    return res


def load_rejects(conn, rejects: list[dict]) -> int:
    run_at = now_utc_us()
    rows = []
    for r in rejects:
        payload = json.dumps(r["payload"], ensure_ascii=False, sort_keys=True, default=str)
        rows.append((r["source"], r["step"], r.get("field"), r["reason"], r.get("synthetic"), payload,
                     hashlib.sha256(payload.encode()).hexdigest(), run_at))
    before = conn.execute("SELECT COUNT(*) FROM rejects").fetchone()[0]
    with conn:
        conn.executemany("INSERT OR IGNORE INTO rejects (source, step, field, reason, synthetic, payload, "
                         "payload_hash, loaded_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    return conn.execute("SELECT COUNT(*) FROM rejects").fetchone()[0] - before


def read_clean():
    loc = pd.read_parquet(CLEAN / "locations.parquet")
    obs = pd.read_parquet(CLEAN / "dataset.parquet")
    return prepare(loc, obs)


def main_load(db_path=DB_PATH, label="load"):
    conn = connect(db_path)
    loc_rows, obs_rows = read_clean()
    res = upsert(conn, loc_rows, obs_rows, label)
    res["rejects_added"] = load_rejects(conn, read_jsonl([CLEAN / "rejects.jsonl"]))
    conn.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


def constraint_checks(conn) -> list[dict]:
    """Попытки нарушить ограничения: каждая должна быть отвергнута БД."""
    cur = conn.execute("SELECT * FROM observations LIMIT 1")
    base = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    tests = [
        ("дубликат первичного ключа (обычный INSERT)", {"observation_time_utc": "2020-01-01T00:00:00+00:00"}),
        ("CHECK: tmax < tmin", {"observation_id": "test_1", "temperature_2m_max": -5.0, "temperature_2m_min": 3.0}),
        ("CHECK: осадки < 0", {"observation_id": "test_2", "precipitation_sum": -1.0}),
        ("CHECK: дата без таймзоны", {"observation_id": "test_3", "observation_time_utc": "2026-10-01T00:00:00"}),
        ("NOT NULL: нет температуры", {"observation_id": "test_4", "temperature_2m_max": None}),
        ("FOREIGN KEY: неизвестная локация", {"observation_id": "test_5", "location_id": "neizvestnograd"}),
        ("UNIQUE: второй замер того же города за те же сутки", {"observation_id": "test_6"}),
    ]
    out = []
    for name, patch in tests:
        rec = {**base, **patch}
        q = f"INSERT INTO observations ({', '.join(rec)}) VALUES ({', '.join('?' * len(rec))})"
        try:
            conn.execute(q, list(rec.values()))
            out.append({"проверка": name, "результат": "ПРИНЯТО (ошибка!)", "сообщение БД": ""})
        except sqlite3.IntegrityError as e:
            out.append({"проверка": name, "результат": "отвергнуто", "сообщение БД": str(e)})
    conn.rollback()
    return out


def demo():
    """Три прогона загрузки на отдельной БД: исходный, повторный, с изменением двух записей."""
    path = DB_DIR / "idempotency_demo.db"
    path.unlink(missing_ok=True)
    conn = connect(path)
    loc_rows, obs_rows = read_clean()

    runs = [upsert(conn, loc_rows, obs_rows, "1. исходный")]
    updated_at_before = dict(conn.execute("SELECT observation_id, updated_at FROM observations"))
    runs.append(upsert(conn, loc_rows, obs_rows, "2. повторный, те же данные"))
    updated_at_after = dict(conn.execute("SELECT observation_id, updated_at FROM observations"))
    same_updated_at = sum(updated_at_before[k] == updated_at_after[k] for k in updated_at_before)

    changed = [dict(r) for r in obs_rows]
    a, b = changed[0], changed[len(changed) // 2]
    a["temperature_2m_max"] = round(a["temperature_2m_max"] + 1.5, 1)
    b["precipitation_sum"] = round((b["precipitation_sum"] or 0) + 2.4, 1)
    runs.append(upsert(conn, loc_rows, changed, "3. изменены две записи"))

    hist = pd.read_sql(
        "SELECT * FROM observations_history WHERE observation_id IN (?, ?) ORDER BY observation_id, valid_from",
        conn, params=(a["observation_id"], b["observation_id"]))
    as_of = runs[1]["run_at"]
    state_q = """SELECT observation_id, temperature_2m_max, precipitation_sum FROM observations_history
                 WHERE observation_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"""
    state = {
        "as_of": as_of,
        "then": conn.execute(state_q, (a["observation_id"], as_of, as_of)).fetchone(),
        "now": conn.execute("SELECT observation_id, temperature_2m_max, precipitation_sum FROM observations "
                            "WHERE observation_id = ?", (a["observation_id"],)).fetchone(),
    }
    checks = constraint_checks(conn)
    conn.close()

    result = {
        "runs": runs,
        "run2_rows_with_same_updated_at": same_updated_at,
        "changed_keys": [a["observation_id"], b["observation_id"]],
        "history_of_changed": hist.to_dict("records"),
        "scd2_state_as_of_run2": state,
        "constraint_checks": checks,
    }
    (DB_DIR / "idempotency_demo.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                                  encoding="utf-8")
    print(pd.DataFrame(runs).drop(columns=["run_at"]).to_string(index=False))
    print(f"\nПрогон 2: updated_at не изменился у {same_updated_at} из {len(updated_at_before)} строк")
    print("\nИстория изменённых записей (SCD2):")
    print(hist.to_string(index=False))
    print(f"\nСостояние {a['observation_id']} на момент прогона 2: {state['then']}; сейчас: {state['now']}")
    print("\nПроверка ограничений:")
    print(pd.DataFrame(checks).to_string(index=False))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    demo() if args.demo else main_load()
