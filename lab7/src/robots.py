"""Чтение robots.txt через urllib.robotparser (RFC 9309).

Отсутствие файла (4xx) по RFC 9309 означает «ограничений нет»; 5xx/недоступность —
«запрещено всё» до следующей проверки. RobotFileParser ведёт себя так же
(allow_all при 4xx, disallow_all при 401/403), а мы дополнительно фиксируем код ответа.
"""
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests


def inspect(base_url: str, paths: list[str], user_agent: str, timeout=10) -> dict:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    status, body = None, ""
    try:
        r = requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=timeout)
        status, body = r.status_code, r.text
        if r.status_code in (401, 403):
            rp.disallow_all = True
        elif 400 <= r.status_code < 500:
            rp.allow_all = True
        elif r.status_code >= 500:
            rp.disallow_all = True
        else:
            rp.parse(body.splitlines())
    except requests.RequestException as e:
        status, body = None, str(e)
        rp.disallow_all = True

    if status and 400 <= status < 500 and status not in (401, 403):
        interpretation = f"файла нет ({status}): по RFC 9309 ограничений нет, работаем по своим лимитам"
    elif status == 200:
        interpretation = "файл найден, правила применены"
    else:
        interpretation = f"robots.txt недоступен ({status}): по RFC 9309 считаем сбор запрещённым"

    return {
        "host": parsed.netloc,
        "robots_url": robots_url,
        "status": status,
        "interpretation": interpretation,
        "can_fetch": {p: rp.can_fetch(user_agent, f"{parsed.scheme}://{parsed.netloc}{p}") for p in paths},
        "crawl_delay": rp.crawl_delay(user_agent),
        "request_rate": (lambda rr: {"requests": rr.requests, "seconds": rr.seconds} if rr else None)(rp.request_rate(user_agent)),
        "sitemaps": rp.site_maps(),
        "raw_preview": body[:300] if status == 200 else None,
    }


def inspect_all(targets: dict, user_agent: str, out="state/robots.json") -> dict:
    """targets: {base_url: [paths]}"""
    res = {urlparse(b).netloc: inspect(b, paths, user_agent) for b, paths in targets.items()}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res
