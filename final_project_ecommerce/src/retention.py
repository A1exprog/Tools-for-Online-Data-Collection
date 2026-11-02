"""Срок хранения сырого слоя (правовой чек-лист, п. 10).

Удаляет разделы raw/<поток>/<дата>.jsonl и снимки HTML старше legal.retention_raw_days.
Чтобы чистый слой не потерял записи, которые с тех пор не менялись, перед удалением
последняя версия каждой записи дописывается в сегодняшний раздел (компакция).

    python -m src.retention --dry-run      # показать, что будет удалено
    python -m src.retention
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .collect_api import STREAM as API_STREAM
from .collect_html import STREAM_LISTING, STREAM_PRODUCT
from .state import latest_by_key

STREAM_KEYS = {STREAM_LISTING: "slug", STREAM_PRODUCT: "slug", API_STREAM: "id"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    raw_dir = Path(cfg["storage"]["raw_dir"])
    days = cfg["legal"].get("retention_raw_days")
    if not days:
        print("retention_raw_days не задан — ничего не удаляем")
        return
    cutoff = date.today() - timedelta(days=int(days))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for stream, key in STREAM_KEYS.items():
        folder = raw_dir / stream
        old = [p for p in folder.glob("*.jsonl") if date.fromisoformat(p.stem) < cutoff] if folder.exists() else []
        if not old:
            continue
        latest = latest_by_key(raw_dir, stream, key)
        old_names = {p.name for p in old}
        to_keep = [env for env in latest.values() if f"{env['fetched_at'][:10]}.jsonl" in old_names]
        print(f"{stream}: удалить {len(old)} файл(ов), перенести {len(to_keep)} актуальных записей в {today}.jsonl")
        if args.dry_run:
            continue
        with (folder / f"{today}.jsonl").open("a", encoding="utf-8") as f:
            for env in to_keep:
                f.write(json.dumps({**env, "compacted_from": env["fetched_at"]}, ensure_ascii=False) + "\n")
        for p in old:
            p.unlink()
    snaps = raw_dir / "html_snapshots"
    for d in snaps.glob("*/*") if snaps.exists() else []:
        try:
            if d.is_dir() and date.fromisoformat(d.name) < cutoff:
                print(f"удалить снимки {d}")
                if not args.dry_run:
                    shutil.rmtree(d)
        except ValueError:
            continue


if __name__ == "__main__":
    main()
