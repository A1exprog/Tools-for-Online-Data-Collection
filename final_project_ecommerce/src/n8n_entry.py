"""Точка входа для n8n (узел Execute Command).

Запускает пайплайн и печатает в stdout ОДНУ JSON-строку со сводкой прогона.
Всегда завершается с кодом 0, чтобы узел n8n не падал; настоящий код
пайплайна передаётся в поле exit_code, и ветвление делает узел IF.

    python -m src.n8n_entry [--config config.yaml] [--simulate-failure api]
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from .run_pipeline import main as run_pipeline


def main() -> None:
    args = sys.argv[1:]
    cfg_path = args[args.index("--config") + 1] if "--config" in args else "config.yaml"
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = run_pipeline(args + ["--quiet"])
        error = None
    except Exception as e:  # пайплайн не должен «ронять» n8n
        code, error = 2, f"{type(e).__name__}: {e}"
    summary = {"exit_code": code, "error": error}
    try:
        cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
        rep = json.loads((Path(cfg["storage"]["reports_dir"]) / "run_report_latest.json").read_text(encoding="utf-8"))
        summary.update({k: rep.get(k) for k in (
            "run_id", "mode", "duration_sec", "requests_total", "status_codes", "error_rate", "not_modified_304",
            "latency_median_ms", "latency_p95_ms", "source_status", "quality_status", "alerts", "incidents")})
        summary["products_clean"] = (rep.get("records") or {}).get("products_clean")
        summary["reviews_clean"] = (rep.get("records") or {}).get("reviews_clean")
        summary["load"] = {k: v for k, v in (rep.get("load") or {}).items() if k in ("products", "reviews", "skipped")}
    except Exception as e:
        summary["error"] = summary["error"] or f"report not found: {e}"
    summary["critical"] = code != 0 or any(a.get("severity") == "critical" for a in summary.get("alerts") or [])
    print(json.dumps(summary, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
