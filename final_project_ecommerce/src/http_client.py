"""Вежливый и устойчивый HTTP-клиент для всех источников.

Что здесь реализовано (требования ТЗ 2.2.3–2.2.5):
* ограничение скорости на домен + джиттер, соблюдение Crawl-delay;
* проверка robots.txt (Disallow) и честный User-Agent с контактом;
* классификация ошибок: временные (429, 5xx, таймауты, обрывы) повторяются
  с экспоненциальной паузой, постоянные (прочие 4xx) — нет;
* приоритет заголовка Retry-After над собственной паузой;
* предохранитель (circuit breaker) на домен — изоляция отказавшего источника;
* условные запросы If-None-Match / If-Modified-Since (ответ 304);
* gzip (Accept-Encoding) и учёт трафика «по сети» и «после распаковки»;
* файловый кэш ответов для отладки (флаг --debug-cache).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import zlib
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from .observability import RunReport, Timer, log_event
from .robots import RobotsTxt

TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Исключения (классификация отказов)
# ---------------------------------------------------------------------------
class FetchError(Exception):
    """Базовая ошибка получения ресурса."""

    kind = "fetch_error"

    def __init__(self, message: str, url: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.url = url
        self.status = status


class PermanentHTTPError(FetchError):
    """4xx (кроме 408/425/429): повтор бессмыслен."""

    kind = "permanent_http_error"


class TransientExhaustedError(FetchError):
    """Временная ошибка не прошла после всех повторов."""

    kind = "transient_exhausted"


class CircuitOpenError(FetchError):
    """Предохранитель домена разомкнут — запросы не отправляются."""

    kind = "circuit_open"


class RobotsDisallowedError(FetchError):
    """URL запрещён robots.txt — не обращаемся."""

    kind = "robots_disallowed"


# ---------------------------------------------------------------------------
# Результат запроса
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    url: str
    status: int
    text: str = ""
    headers: dict = field(default_factory=dict)
    not_modified: bool = False
    from_cache: bool = False
    elapsed_ms: float = 0.0

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Предохранитель
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """CLOSED -> (N подряд отказов) -> OPEN -> (cooldown) -> HALF_OPEN -> 1 проба."""

    def __init__(self, failure_threshold: int = 5, cooldown_sec: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self.clock = clock
        self.failures = 0
        self.state = "CLOSED"
        self.opened_at = 0.0

    def allow(self) -> bool:
        if self.state == "OPEN":
            if self.clock() - self.opened_at >= self.cooldown_sec:
                self.state = "HALF_OPEN"
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = "CLOSED"

    def record_failure(self) -> bool:
        """Возвращает True, если предохранитель только что разомкнулся."""
        self.failures += 1
        if self.state == "HALF_OPEN" or self.failures >= self.failure_threshold:
            just_opened = self.state != "OPEN"
            self.state = "OPEN"
            self.opened_at = self.clock()
            return just_opened
        return False


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------
@dataclass
class DomainPolicy:
    min_interval: float = 1.0
    jitter: float = 0.5
    last_request: float = 0.0
    crawl_delay: Optional[float] = None
    robots: Optional[RobotsTxt] = None
    robots_loaded: bool = False
    sitemaps: list = field(default_factory=list)


class PoliteClient:
    def __init__(self, cfg: dict, logger: logging.Logger, report: RunReport, state_dir: Path,
                 debug_cache: bool = False, sleep: Callable[[float], None] = time.sleep):
        http = cfg["http"]
        self.logger = logger
        self.report = report
        self.sleep = sleep
        self.user_agent = http["user_agent"]
        self.timeout = http["timeout_sec"]
        self.max_retries = int(http["max_retries"])
        self.backoff_base = float(http["backoff_base_sec"])
        self.max_retry_after = float(http["max_retry_after_sec"])
        self.respect_robots = bool(http.get("respect_robots", True))
        # Явные, задокументированные исключения из robots.txt (см. docs/legal_checklist.md, п. 4)
        self.robots_exceptions: list[dict] = list(http.get("robots_exceptions") or [])
        self._exceptions_logged: set[str] = set()
        cb = http["circuit_breaker"]
        self._cb_cfg = (int(cb["failure_threshold"]), float(cb["cooldown_sec"]))
        self.breakers: dict[str, CircuitBreaker] = {}
        self.domains: dict[str, DomainPolicy] = {}

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate" if http.get("accept_gzip", True) else "identity",
        })

        # Валидаторы для условных запросов (ETag / Last-Modified) + хэш содержимого
        self.state_path = state_dir / "etags.json"
        self.validators: dict[str, dict] = {}
        if self.state_path.exists():
            self.validators = json.loads(self.state_path.read_text(encoding="utf-8"))

        dc = http.get("debug_cache", {})
        self.cache_enabled = debug_cache
        self.cache_dir = Path(dc.get("dir", ".cache/http"))
        self.cache_ttl = float(dc.get("ttl_sec", 86400))

    # --- настройка доменов ------------------------------------------------
    def configure_domain(self, url: str, min_interval: float, jitter: float) -> None:
        d = self._domain(url)
        pol = self.domains.setdefault(d, DomainPolicy())
        pol.min_interval = float(min_interval)
        pol.jitter = float(jitter)

    @staticmethod
    def _domain(url: str) -> str:
        return urlparse(url).netloc.lower()

    def _breaker(self, domain: str) -> CircuitBreaker:
        if domain not in self.breakers:
            self.breakers[domain] = CircuitBreaker(*self._cb_cfg)
        return self.breakers[domain]

    def breaker_states(self) -> dict:
        return {d: b.state for d, b in self.breakers.items()}

    # --- вежливость: пауза на домен -----------------------------------------
    def _throttle(self, domain: str) -> None:
        pol = self.domains.setdefault(domain, DomainPolicy())
        interval = max(pol.min_interval, pol.crawl_delay or 0.0)
        wait = pol.last_request + interval + random.uniform(0, pol.jitter) - time.monotonic()
        if wait > 0 and pol.last_request > 0:
            self.sleep(wait)
        pol.last_request = time.monotonic()

    # --- robots.txt -----------------------------------------------------------
    def load_robots(self, url: str, source: str) -> DomainPolicy:
        domain = self._domain(url)
        pol = self.domains.setdefault(domain, DomainPolicy())
        if pol.robots_loaded:
            return pol
        pol.robots_loaded = True
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            res = self._request(robots_url, source=source, conditional=False, check_robots=False)
            rp = RobotsTxt(res.text)
            pol.crawl_delay = rp.crawl_delay(self.user_agent)
            pol.sitemaps = list(rp.sitemaps)
            log_event(self.logger, "robots_loaded", source=source, url=robots_url,
                      crawl_delay=pol.crawl_delay, sitemaps=pol.sitemaps,
                      rules=[r for g in rp._groups_for(self.user_agent) for r in g.rules])
        except PermanentHTTPError as e:  # RFC 9309: 4xx — ограничений нет
            rp = RobotsTxt(allow_all=True)
            log_event(self.logger, "robots_absent", source=source, url=robots_url, status=e.status)
        except FetchError as e:
            # RFC 9309: robots.txt недоступен (5xx, сеть) — считаем всё запрещённым
            rp = RobotsTxt(disallow_all=True)
            log_event(self.logger, "robots_unreachable", logging.WARNING, source=source,
                      url=robots_url, error=str(e))
        pol.robots = rp
        return pol

    def can_fetch(self, url: str) -> bool:
        pol = self.domains.get(self._domain(url))
        if not self.respect_robots or pol is None or pol.robots is None:
            return True
        return pol.robots.can_fetch(self.user_agent, url)

    def _robots_exception(self, url: str) -> Optional[dict]:
        for exc in self.robots_exceptions:
            if exc.get("url_prefix") and url.startswith(exc["url_prefix"]):
                return exc
        return None

    # --- отладочный кэш ---------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")

    def _cache_get(self, key: str) -> Optional[FetchResult]:
        if not self.cache_enabled:
            return None
        p = self._cache_path(key)
        if p.exists() and time.time() - p.stat().st_mtime < self.cache_ttl:
            d = json.loads(p.read_text(encoding="utf-8"))
            return FetchResult(url=d["url"], status=d["status"], text=d["text"],
                               headers=d["headers"], from_cache=True)
        return None

    def _cache_put(self, key: str, res: FetchResult) -> None:
        if not self.cache_enabled or res.status != 200:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(json.dumps({
            "url": res.url, "status": res.status, "text": res.text, "headers": res.headers,
        }, ensure_ascii=False), encoding="utf-8")

    # --- условные запросы -------------------------------------------------------
    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.validators, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)  # атомарная замена — файл не побьётся при прерывании

    def is_same_content(self, key: str, sha256: str) -> bool:
        """Сервер не поддерживает ETag, но содержимое не изменилось."""
        return self.validators.get(key, {}).get("sha256") == sha256

    def remember(self, key: str, res: FetchResult) -> None:
        v = self.validators.setdefault(key, {})
        if res.headers.get("ETag"):
            v["etag"] = res.headers["ETag"]
        if res.headers.get("Last-Modified"):
            v["last_modified"] = res.headers["Last-Modified"]
        v["sha256"] = res.content_sha256
        v["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- основной метод ------------------------------------------------------------
    def get(self, url: str, *, source: str, params: Optional[dict] = None,
            conditional: bool = True) -> FetchResult:
        return self._request(url, source=source, params=params, conditional=conditional)

    @staticmethod
    def full_url(url: str, params: Optional[dict]) -> str:
        return requests.Request("GET", url, params=params).prepare().url or url

    def _retry_after(self, resp: requests.Response) -> Optional[float]:
        val = resp.headers.get("Retry-After")
        if not val:
            return None
        try:
            sec = float(val)
        except ValueError:
            try:
                sec = (parsedate_to_datetime(val) - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                return None
        return max(0.0, min(sec, self.max_retry_after))

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base)

    def _request(self, url: str, *, source: str, params: Optional[dict] = None,
                 conditional: bool = True, check_robots: bool = True) -> FetchResult:
        key = self.full_url(url, params)
        domain = self._domain(key)

        cached = self._cache_get(key)
        if cached is not None:
            self.report.add_cache_hit(source)
            log_event(self.logger, "http_cache_hit", source=source, url=key)
            return cached

        if check_robots and self.respect_robots:
            self.load_robots(key, source)
            if not self.can_fetch(key):
                exc = self._robots_exception(key)
                if exc is None:
                    log_event(self.logger, "robots_disallowed", logging.WARNING, source=source, url=key)
                    raise RobotsDisallowedError("disallowed by robots.txt", url=key)
                if exc["url_prefix"] not in self._exceptions_logged:  # фиксируем один раз за прогон
                    self._exceptions_logged.add(exc["url_prefix"])
                    pol = self.domains.get(domain)
                    rule = pol.robots.matching_rule(self.user_agent, key) if pol and pol.robots else None
                    entry = {"url_prefix": exc["url_prefix"], "robots_rule": rule[1] if rule else None,
                             "reason": exc.get("reason"), "approved_by": exc.get("approved_by")}
                    self.report.robots_exceptions.append(entry)
                    log_event(self.logger, "robots_exception_applied", logging.WARNING, source=source,
                              url=key, **entry)

        breaker = self._breaker(domain)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            if not breaker.allow():
                log_event(self.logger, "circuit_open_skip", logging.WARNING, source=source,
                          url=key, domain=domain)
                raise CircuitOpenError(f"circuit open for {domain}", url=key)

            headers = {}
            if conditional:
                v = self.validators.get(key, {})
                if v.get("etag"):
                    headers["If-None-Match"] = v["etag"]
                if v.get("last_modified"):
                    headers["If-Modified-Since"] = v["last_modified"]

            self._throttle(domain)
            status: Optional[int] = None
            resp: Optional[requests.Response] = None
            with Timer() as t:
                try:
                    resp, wire = self._send(url, params, headers)
                    status = resp.status_code
                except requests.RequestException as e:
                    last_error = f"{type(e).__name__}: {e}"

            body = len(resp.content) if resp is not None else 0
            if resp is None:
                wire = 0
            self.report.add_request(source, status, t.elapsed_ms, body, wire)
            log_event(self.logger, "http_request", source=source, url=key, status=status,
                      attempt=attempt, latency_ms=round(t.elapsed_ms, 1), bytes=body, wire_bytes=wire,
                      encoding=resp.headers.get("Content-Encoding") if resp is not None else None,
                      error=last_error if resp is None else None)

            # --- успех / 304 ---
            if status is not None and (200 <= status < 300 or status == 304):
                breaker.record_success()
                res = FetchResult(url=key, status=status, text=resp.text if status != 304 else "",
                                  headers=dict(resp.headers), not_modified=(status == 304),
                                  elapsed_ms=t.elapsed_ms)
                if status == 200:
                    self._cache_put(key, res)
                return res

            # --- постоянная ошибка: не повторяем ---
            if status is not None and status not in TRANSIENT_STATUSES:
                breaker.record_success()  # сервер жив, просто ресурс «не тот»
                raise PermanentHTTPError(f"HTTP {status}", url=key, status=status)

            # --- временная ошибка: повтор ---
            if breaker.record_failure():
                log_event(self.logger, "circuit_opened", logging.ERROR, source=source,
                          domain=domain, failures=breaker.failures)
            if attempt >= self.max_retries:
                break
            wait = self._retry_after(resp) if resp is not None else None
            reason = "retry_after" if wait is not None else "backoff"
            if wait is None:
                wait = self._backoff(attempt)
            self.report.add_retry(source)
            log_event(self.logger, "http_retry", logging.WARNING, source=source, url=key,
                      status=status, error=last_error or None, attempt=attempt + 1,
                      wait_sec=round(wait, 2), wait_reason=reason)
            self.sleep(wait)

        raise TransientExhaustedError(
            f"gave up after {self.max_retries + 1} attempts: {last_error or 'HTTP ' + str(status)}",
            url=key, status=status)

    def _send(self, url: str, params: Optional[dict], headers: dict) -> tuple[requests.Response, int]:
        """GET с подсчётом трафика «по сети».

        Тело читается в сжатом виде (decode_content=False) и распаковывается здесь же:
        так видно реальный объём gzip-ответа, в т.ч. при Transfer-Encoding: chunked,
        когда заголовка Content-Length нет.
        """
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout, stream=True)
        raw_reader = getattr(resp.raw, "read", None)
        if raw_reader is None:  # подменённая сессия в тестах
            return resp, len(resp.content)
        try:
            raw = raw_reader(decode_content=False)
        except Exception as e:  # обрыв/таймаут при чтении тела -> временная сетевая ошибка
            resp.close()
            raise requests.ConnectionError(f"body read failed: {e}") from e
        finally:
            resp.close()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        body = raw
        if raw and enc in ("gzip", "x-gzip", "deflate"):
            try:
                body = zlib.decompress(raw, 47)          # gzip или zlib (автоопределение)
            except zlib.error:
                try:
                    body = zlib.decompress(raw, -15)     # «сырой» deflate
                except zlib.error as e:
                    raise requests.exceptions.ContentDecodingError(f"cannot decode {enc}: {e}") from e
        resp._content = body
        resp._content_consumed = True
        return resp, len(raw)
