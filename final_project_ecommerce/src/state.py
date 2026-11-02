"""Сырой слой (append-only JSON Lines) и состояние для инкрементальности.

raw/<stream>/<YYYY-MM-DD>.jsonl — только дозапись, каждая строка — «конверт»
с метаданными происхождения и полезной нагрузкой (payload).

state/watermark.json  — водяные знаки (что и в каком состоянии уже собрано)
state/checkpoint.json — прогресс текущего прогона (возобновляемость)
state/etags.json      — ETag / Last-Modified / sha256 страниц (см. http_client)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


def stable_hash(obj: Any) -> str:
    """Детерминированный хэш содержимого записи (для дедупликации и UPSERT)."""
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Сырой слой
# ---------------------------------------------------------------------------
class RawWriter:
    """Дописывает записи в raw/<stream>/<дата>.jsonl. Никогда не перезаписывает."""

    def __init__(self, raw_dir: Path, run_id: str, parser_version: str):
        self.raw_dir = raw_dir
        self.run_id = run_id
        self.parser_version = parser_version
        self.counts: dict[str, int] = {}

    def append(self, stream: str, source: str, url: str, payload: dict,
               http_status: Optional[int] = 200, page_sha256: Optional[str] = None) -> dict:
        now = datetime.now(timezone.utc)
        envelope = {
            "stream": stream,
            "run_id": self.run_id,
            "source": source,
            "url": url,
            "fetched_at": now.isoformat(timespec="seconds"),
            "http_status": http_status,
            "page_sha256": page_sha256,
            "parser_version": self.parser_version,
            "record_hash": stable_hash(payload),
            "payload": payload,
        }
        path = self.raw_dir / stream / f"{now:%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:  # режим "a" — только дозапись
            f.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
        self.counts[stream] = self.counts.get(stream, 0) + 1
        return envelope


def iter_raw(raw_dir: Path, stream: str) -> Iterator[dict]:
    """Все конверты потока в хронологическом порядке (битые строки пропускаются)."""
    folder = raw_dir / stream
    if not folder.exists():
        return
    for path in sorted(folder.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # строка, оборванная при аварийном завершении


def latest_by_key(raw_dir: Path, stream: str, key: str) -> dict[str, dict]:
    """Последняя версия каждой записи по ключу payload[key]."""
    out: dict[str, dict] = {}
    for env in iter_raw(raw_dir, stream):
        k = env.get("payload", {}).get(key)
        if k is not None:
            out[str(k)] = env
    return out


# ---------------------------------------------------------------------------
# JSON-состояние с атомарной записью
# ---------------------------------------------------------------------------
class JsonState:
    def __init__(self, path: Path, default: dict):
        self.path = path
        self.data = default
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.path)


def load_watermark(state_dir: Path) -> JsonState:
    return JsonState(state_dir / "watermark.json", {"html": {"details": {}, "sitemap": {}},
                                                    "api": {"products": {}, "max_updated_at": None}})


class Checkpoint(JsonState):
    """Прогресс прогона. status=running после аварии => следующий запуск продолжит."""

    def __init__(self, state_dir: Path):
        super().__init__(state_dir / "checkpoint.json", {"status": "none"})

    def begin(self, run_id: str, fresh: bool) -> Optional[str]:
        """Начать прогон. Возвращает run_id прерванного прогона, если продолжаем его."""
        resumed = None
        if not fresh and self.data.get("status") == "running":
            resumed = self.data.get("run_id")
            self.data["resumed_by"] = run_id
        else:
            self.data = {"run_id": run_id, "status": "running",
                         "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "html_pages_done": [], "api_pages_done": []}
        self.data["status"] = "running"
        self.save()
        return resumed

    def done_set(self, name: str) -> set:
        return set(self.data.get(name, []))

    def mark(self, name: str, value: Any) -> None:
        lst = self.data.setdefault(name, [])
        if value not in lst:
            lst.append(value)
        self.save()

    def finish(self, status: str = "done") -> None:
        self.data["status"] = status
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()
