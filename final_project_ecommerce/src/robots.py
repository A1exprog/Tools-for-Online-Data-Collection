"""Разбор robots.txt по RFC 9309.

Стандартный urllib.robotparser в распространённых версиях Python (проверено на 3.10)
НЕ поддерживает подстановочные символы `*` и `$`: правило `Disallow: /ecommerce/*`
он воспринимает буквально и «разрешает» `/ecommerce/page/2/`. Поведение зависит от
версии интерпретатора, поэтому здесь своя реализация, не зависящая от версии:

* группы по `User-agent` (совпадение по продуктовому токену, иначе группа `*`);
* `*` — любая последовательность символов, `$` в конце — конец пути;
* побеждает самое длинное совпавшее правило; при равной длине — Allow;
* `/robots.txt` всегда разрешён; пустой `Disallow:` ничего не запрещает;
* `Crawl-delay` (нестандартное, но распространённое) и `Sitemap`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlparse


@dataclass
class _Group:
    agents: list = field(default_factory=list)
    rules: list = field(default_factory=list)       # (allow: bool, pattern: str)
    crawl_delay: Optional[float] = None


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""))


class RobotsTxt:
    def __init__(self, text: str = "", allow_all: bool = False, disallow_all: bool = False):
        self.groups: list[_Group] = []
        self.sitemaps: list[str] = []
        self.allow_all = allow_all
        self.disallow_all = disallow_all
        if text:
            self._parse(text)

    def _parse(self, text: str) -> None:
        current: Optional[_Group] = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key == "sitemap":
                self.sitemaps.append(value)
            elif current is None:
                continue
            elif key in ("allow", "disallow"):
                if value:  # пустой Disallow ничего не запрещает
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass

    def _groups_for(self, user_agent: str) -> list[_Group]:
        token = user_agent.split("/")[0].strip().lower()
        specific = [g for g in self.groups if any(a != "*" and a in token for a in g.agents)]
        return specific or [g for g in self.groups if "*" in g.agents]

    def matching_rule(self, user_agent: str, url: str) -> Optional[tuple[bool, str]]:
        """Правило, которое решает судьбу URL (allow, pattern), или None."""
        u = urlparse(url)
        path = unquote(u.path or "/") + (f"?{u.query}" if u.query else "")
        best: Optional[tuple[bool, str]] = None
        for g in self._groups_for(user_agent):
            for allow, pattern in g.rules:
                if _compile(pattern).match(path):
                    if (best is None or len(pattern) > len(best[1])
                            or (len(pattern) == len(best[1]) and allow)):
                        best = (allow, pattern)
        return best

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if urlparse(url).path == "/robots.txt" or self.allow_all:
            return True
        if self.disallow_all:
            return False
        rule = self.matching_rule(user_agent, url)
        return True if rule is None else rule[0]

    def crawl_delay(self, user_agent: str) -> Optional[float]:
        delays = [g.crawl_delay for g in self._groups_for(user_agent) if g.crawl_delay is not None]
        return max(delays) if delays else None
