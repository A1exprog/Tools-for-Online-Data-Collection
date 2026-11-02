"""Управление скоростью: token bucket с джиттером + адаптивная задержка, лимиты по доменам.

DomainLimiter      — «домен → ограничитель» внутри одного процесса.
SharedDomainLimiter — общий счётчик для нескольких процессов (файл + блокировка ОС):
                      4 параллельных процесса вместе не превышают лимит домена.
"""
import json
import os
import random
import threading
import time
from pathlib import Path


class TokenBucket:
    """rate — токенов в секунду, burst — ёмкость ведра (сколько запросов можно подряд)."""

    def __init__(self, rate: float, burst: int = 1):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def reserve(self) -> float:
        """Забирает токен; возвращает, сколько секунд нужно подождать до запроса."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            self.tokens -= 1
            if self.tokens >= 0:
                return 0.0
            return -self.tokens / self.rate          # токен «в долг»: ждём, пока он накопится


class AdaptiveDelay:
    """Минимальный интервал между запросами к домену по измеренной латентности.

    interval = max(base_interval, k × EWMA(латентность)); при 429/503 интервал умножается
    на backoff_factor (до max_interval), после успешных ответов плавно возвращается к норме.
    """

    def __init__(self, base_interval: float, k: float = 2.0, alpha: float = 0.3,
                 backoff_factor: float = 2.0, max_interval: float = 30.0):
        self.base = base_interval
        self.k = k
        self.alpha = alpha
        self.factor = backoff_factor
        self.max = max_interval
        self.ewma = None
        self.penalty = 1.0

    def observe(self, latency_s: float, status: int | None):
        if latency_s is not None and latency_s > 0:
            self.ewma = latency_s if self.ewma is None else self.alpha * latency_s + (1 - self.alpha) * self.ewma
        if status in (429, 503):
            self.penalty = min(self.penalty * self.factor, self.max / max(self.base, 1e-6))
        elif status is not None and status < 400:
            self.penalty = max(1.0, self.penalty * 0.75)

    @property
    def interval(self) -> float:
        lat_part = self.k * self.ewma if self.ewma else 0.0
        return min(self.max, max(self.base, lat_part) * self.penalty)


class DomainLimiter:
    def __init__(self, rate: float, burst: int, jitter=(0.2, 0.3), adaptive: dict | None = None,
                 per_domain: dict | None = None):
        self.rate, self.burst, self.jitter = rate, burst, jitter
        self.adaptive_cfg = adaptive or {}
        self.per_domain = per_domain or {}           # domain → {"rate":…, "burst":…}
        self.buckets: dict[str, TokenBucket] = {}
        self.adaptive: dict[str, AdaptiveDelay] = {}
        self.last_request: dict[str, float] = {}
        self.lock = threading.Lock()

    def set_crawl_delay(self, domain: str, delay: float | None):
        """Crawl-delay из robots.txt важнее собственных настроек: rate ≤ 1/delay."""
        if delay:
            cfg = self.per_domain.setdefault(domain, {})
            cfg["rate"] = min(cfg.get("rate", self.rate), 1.0 / float(delay))
            cfg["burst"] = 1
            self.buckets.pop(domain, None)

    def _get(self, domain):
        with self.lock:
            if domain not in self.buckets:
                c = self.per_domain.get(domain, {})
                rate = c.get("rate", self.rate)
                self.buckets[domain] = TokenBucket(rate, c.get("burst", self.burst))
                self.adaptive[domain] = AdaptiveDelay(base_interval=1.0 / rate, **self.adaptive_cfg)
            return self.buckets[domain], self.adaptive[domain]

    def wait(self, domain: str) -> dict:
        """Блокирует до разрешённого момента; возвращает разложение задержки для лога."""
        bucket, adaptive = self._get(domain)
        bucket_wait = bucket.reserve()
        since_last = time.monotonic() - self.last_request.get(domain, 0.0)
        adaptive_wait = max(0.0, adaptive.interval - since_last)
        base = max(bucket_wait, adaptive_wait)
        jitter = base * random.uniform(*self.jitter) if base > 0 else 0.0
        total = base + jitter
        if total > 0:
            time.sleep(total)
        self.last_request[domain] = time.monotonic()
        return {"wait_s": round(total, 3), "bucket_s": round(bucket_wait, 3),
                "adaptive_s": round(adaptive_wait, 3), "interval_s": round(adaptive.interval, 3)}

    def observe(self, domain: str, latency_s: float | None, status: int | None):
        self._get(domain)[1].observe(latency_s, status)


# ---------------------------------------------------------------------------
# Общий лимит для нескольких процессов: файл state/locks/<domain>.json хранит момент,
# раньше которого следующий запрос к домену отправлять нельзя. Чтение-изменение-запись
# выполняется под блокировкой файла ОС, поэтому процессы не обгоняют друг друга.
# ---------------------------------------------------------------------------
if os.name == "nt":
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class SharedDomainLimiter:
    def __init__(self, lock_dir="state/locks", rate: float = 1.0, jitter=(0.2, 0.3)):
        self.dir = Path(lock_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rate = rate
        self.jitter = jitter

    def wait(self, domain: str) -> float:
        path = self.dir / f"{domain.replace(':', '_')}.json"
        with open(path, "a+", encoding="utf-8") as f:
            _lock(f)
            try:
                f.seek(0)
                raw = f.read().strip()
                next_ok = json.loads(raw)["next_ok"] if raw else 0.0
                now = time.time()
                slot = max(now, next_ok)
                interval = (1.0 / self.rate) * (1 + random.uniform(*self.jitter))
                f.seek(0)
                f.truncate()
                f.write(json.dumps({"next_ok": slot + interval}))
                f.flush()
            finally:
                _unlock(f)
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)
