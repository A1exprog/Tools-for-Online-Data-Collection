"""Политика повторов: классификация ошибок, экспоненциальная задержка с full jitter,
кэп задержки, лимит попыток, бюджет времени на URL, приоритет Retry-After."""
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

# класс → (повторять?, что делаем)
CLASSES = {
    "ok":              (False, "успех"),
    "not_modified":    (False, "304: ресурс не изменился, берём сохранённую копию"),
    "rate_limited":    (True,  "429: повтор после Retry-After или backoff, интервал домена увеличивается"),
    "server_error":    (True,  "5xx: временный сбой сервера, повтор с backoff"),
    "timeout":         (True,  "таймаут соединения/чтения/ожидания селектора, повтор с backoff"),
    "connection":      (True,  "обрыв соединения, DNS, сброс — повтор с backoff"),
    "auth_expired":    (False, "401: без повторов; один раз обновить токен, иначе остановка"),
    "possible_block":  (False, "403: без повторов; фиксируем возможную блокировку, инцидент"),
    "client_error":    (False, "прочие 4xx (404, 410, 400): ошибка запроса, повтор бессмыслен"),
    "content_block":   (False, "детектор: маркеры блокировки в ответе — остановка домена"),
    "unknown":         (False, "неизвестная ошибка — без повторов, в лог"),
}


def classify(status: int | None = None, exc: BaseException | None = None) -> str:
    if exc is not None:
        name = type(exc).__name__.lower()
        if isinstance(exc, (requests.Timeout, TimeoutError)) or "timeout" in name:
            return "timeout"
        if isinstance(exc, (requests.ConnectionError, ConnectionError)):
            return "connection"
        return "unknown"
    if status is None:
        return "unknown"
    if status == 304:
        return "not_modified"
    if 200 <= status < 400:
        return "ok"
    if status == 429:
        return "rate_limited"
    if status == 401:
        return "auth_expired"
    if status == 403:
        return "possible_block"
    if status in (408,):
        return "timeout"
    if 500 <= status < 600:
        return "server_error"
    if 400 <= status < 500:
        return "client_error"
    return "unknown"


def should_retry(error_class: str) -> bool:
    return CLASSES.get(error_class, (False, ""))[0]


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After: секунды ('120') или HTTP-дата ('Wed, 21 Oct 2026 07:28:00 GMT')."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


@dataclass
class RetryPolicy:
    max_attempts: int = 4          # всего попыток, включая первую
    base: float = 1.0              # с
    cap: float = 30.0              # максимум одной задержки backoff
    retry_after_cap: float = 120.0  # Retry-After больше этого — не ждём, а сдаёмся (бюджет)
    url_budget: float = 60.0       # общий бюджет времени на один URL, с

    def backoff(self, attempt: int) -> float:
        """Full jitter (AWS): U(0, min(cap, base·2^(attempt−1)))."""
        return random.uniform(0, min(self.cap, self.base * 2 ** (attempt - 1)))

    def next_delay(self, attempt: int, retry_after: str | None) -> tuple[float, str]:
        ra = parse_retry_after(retry_after)
        if ra is not None:
            return min(ra, self.retry_after_cap), "Retry-After"
        return self.backoff(attempt), "backoff"
