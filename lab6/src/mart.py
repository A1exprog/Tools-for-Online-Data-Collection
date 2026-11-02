"""Слой mart: аналитические представления, партиционированные снапшоты, графики.

Строится только после зелёного/жёлтого светофора (clean/quality_verdict.json).

    python src/mart.py
"""
import json
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from common import CLEAN, DB_PATH, MART  # noqa: E402
from quality import check_gate  # noqa: E402

VIEWS = {
    "city_stats": "SELECT * FROM v_city_stats ORDER BY city",
    "daily_dynamics": "SELECT * FROM v_daily_dynamics",
    "top_warmest": "SELECT * FROM v_top_warmest",
}


def build():
    meta = json.loads((CLEAN / "quality_verdict.json").read_text(encoding="utf-8"))
    check_gate(meta)  # при красном статусе здесь будет исключение

    (MART / "views").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        views = {name: pd.read_sql(q, conn) for name, q in VIEWS.items()}
    for name, df in views.items():
        df.to_parquet(MART / "views" / f"{name}.parquet", index=False)
        print(f"mart/views/{name}.parquet: {len(df)} строк")

    # снапшоты чистого слоя, партиционированные по дате сбора: mart/snapshots/dt=YYYY-MM-DD/
    obs = pd.read_parquet(CLEAN / "dataset.parquet")
    obs["dt"] = obs["collected_at"].dt.strftime("%Y-%m-%d")
    obs.to_parquet(MART / "snapshots", partition_cols=["dt"], index=False,
                   existing_data_behavior="delete_matching")   # повторный прогон за ту же дату перезаписывает партицию
    parts = sorted(p.name for p in (MART / "snapshots").iterdir() if p.is_dir())
    print("Партиции:", parts)
    one = pd.read_parquet(MART / "snapshots" / parts[-1])
    print(f"Чтение одной партиции {parts[-1]}: {len(one)} строк")

    charts(obs, views["daily_dynamics"], views["top_warmest"])
    return views, parts


def charts(obs, daily, top):
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.5))
    ax[0].hist(obs["temperature_2m_max"], bins=30, alpha=0.75, color="#C8553D", label="максимум")
    ax[0].hist(obs["temperature_2m_min"], bins=30, alpha=0.75, color="#2F5D8A", label="минимум")
    ax[0].set_title("Распределение суточных температур, °C")
    ax[0].set_xlabel("°C"); ax[0].set_ylabel("наблюдений"); ax[0].legend()
    d = pd.to_datetime(daily["day"])
    ax[1].plot(d, daily["avg_tmax"], marker="o", ms=3, color="#C8553D", label="средний максимум")
    ax[1].plot(d, daily["avg_tmin"], marker="o", ms=3, color="#2F5D8A", label="средний минимум")
    ax[1].set_title("Динамика средней температуры по всем городам"); ax[1].set_ylabel("°C")
    ax[1].tick_params(axis="x", rotation=45); ax[1].legend()
    plt.tight_layout(); fig.savefig(MART / "temperature_charts.png", dpi=110); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    t = top.sort_values("avg_tmax")
    ax.barh(t["city"], t["avg_tmax"], color="#C8553D")
    ax.set_title("Топ-10 самых тёплых городов (средний суточный максимум)"); ax.set_xlabel("°C")
    plt.tight_layout(); fig.savefig(MART / "top_warmest.png", dpi=110); plt.close(fig)
    print("Графики: mart/temperature_charts.png, mart/top_warmest.png")


if __name__ == "__main__":
    build()
