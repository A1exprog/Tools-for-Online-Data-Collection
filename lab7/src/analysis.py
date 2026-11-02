"""Графики по структурным журналам: распределение латентности, коды ответов во времени,
работа предохранителя."""
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from observability import read_log  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOGS, REPORTS = ROOT / "logs", ROOT / "reports"
RUNS = ["A", "B", "C_cold", "C_warm"]
COLORS = {"A": "#C8553D", "B": "#2F5D8A", "C_cold": "#5B8E7D", "C_warm": "#9AA3AE"}


def requests_of(run):
    p = LOGS / f"run_{run}.jsonl"
    return [e for e in read_log(p) if e["event"] == "request"] if p.exists() else []


def latency_hist():
    fig, ax = plt.subplots(figsize=(9, 4))
    for r in RUNS:
        ms = [e["ms"] for e in requests_of(r) if e["ms"] is not None]
        if ms:
            ax.hist(ms, bins=20, alpha=0.55, label=f"{r} (n={len(ms)})", color=COLORS[r])
    ax.set_xlabel("латентность, мс"); ax.set_ylabel("запросов"); ax.set_title("Распределение латентности по прогонам")
    ax.legend(); fig.tight_layout(); fig.savefig(REPORTS / "latency_hist.png", dpi=110); plt.close(fig)


def code_group(e):
    s = e["status"]
    if s is None:
        return "исключение/таймаут"
    return {2: "2xx", 3: "304/3xx", 4: "4xx", 5: "5xx"}[s // 100]


def codes_timeline(run, fname, title):
    ev = read_log(LOGS / f"run_{run}.jsonl")
    reqs = [e for e in ev if e["event"] == "request"]
    if not reqs:
        return
    t0 = datetime.fromisoformat(ev[0]["ts"])
    groups = ["2xx", "304/3xx", "4xx", "5xx", "исключение/таймаут"]
    colors = ["#5B8E7D", "#9AA3AE", "#E0A458", "#C8553D", "#6C4F70"]
    fig, ax = plt.subplots(figsize=(10, 4))
    for g, col in zip(groups, colors):
        xs = [(datetime.fromisoformat(e["ts"]) - t0).total_seconds() for e in reqs if code_group(e) == g]
        ys = [e["host"] for e in reqs if code_group(e) == g]
        if xs:
            ax.scatter(xs, ys, label=g, color=col, s=36)
    for e in ev:
        if e["event"] == "breaker_state":
            x = (datetime.fromisoformat(e["ts"]) - t0).total_seconds()
            ax.axvline(x, color="#C8553D" if e["new"] == "open" else "#5B8E7D", ls="--", lw=1)
            ax.text(x + 0.5, 0.5, e["new"], transform=ax.get_xaxis_transform(), fontsize=8, rotation=90, va="center")
        if e["event"] == "breaker_skip":
            x = (datetime.fromisoformat(e["ts"]) - t0).total_seconds()
            ax.scatter([x], [e["host"]], marker="x", color="#333333", s=30)
    ax.set_xlabel("секунды от начала прогона"); ax.set_title(title)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8)
    fig.tight_layout(); fig.savefig(REPORTS / fname, dpi=110); plt.close(fig)


def main():
    REPORTS.mkdir(exist_ok=True)
    latency_hist()
    codes_timeline("B", "codes_timeline_B.png", "Прогон B: ответы по времени и доменам")
    if (LOGS / "run_S4_breaker_isolation.jsonl").exists():
        codes_timeline("S4_breaker_isolation", "codes_timeline_breaker.png",
                       "Предохранитель: стенд (503) отключён, quotes.toscrape.com продолжает (× — пропуск)")
    print("Графики: reports/latency_hist.png, codes_timeline_B.png, codes_timeline_breaker.png")


if __name__ == "__main__":
    main()
