"""Общие пути и функции нормализации."""
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw"
CLEAN = ROOT / "clean"
MART = ROOT / "mart"
DB_DIR = ROOT / "db"
DB_PATH = DB_DIR / "warehouse.db"

RAW_FILES = {
    "time-in.ru": [RAW / "locations_html.jsonl", RAW / "locations_html_synthetic.jsonl"],
    "open-meteo": [RAW / "weather_api.jsonl", RAW / "weather_api_synthetic.jsonl"],
}

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def now_utc() -> str:
    """Текущее время UTC в ISO 8601 с явным смещением +00:00."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(s):
    """Шаг 1: NFKC, \\xa0 → пробел, удаление zero-width, схлопывание пробелов, обрезка."""
    if not isinstance(s, str):
        return s
    s = unicodedata.normalize("NFKC", s).replace("\xa0", " ").translate(ZERO_WIDTH)
    return re.sub(r"\s+", " ", s).strip()


def slugify(s: str) -> str:
    """Стабильный ASCII-ключ: «Нижний Новгород» → nizhniy-novgorod, «Тимашёвск» → timashevsk."""
    s = normalize_text(str(s)).lower()
    s = "".join(TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


TRACKING = re.compile(r"(?:utm_[^=&]+|ref|fbclid|gclid|yclid)=[^&]*&?")


def strip_tracking(url):
    """Убирает параметры трекинга (utm_*, ref, fbclid…) из URL."""
    if not isinstance(url, str):
        return url
    if "?" not in url:
        return url
    base, q = url.split("?", 1)
    q = TRACKING.sub("", q).strip("&")
    return f"{base}?{q}" if q else base


def read_jsonl(paths) -> list[dict]:
    rows = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows, mode="w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
