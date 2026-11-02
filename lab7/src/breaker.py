"""Предохранитель (circuit breaker) на уровне домена: closed → open → half-open → closed.

closed     — запросы идут, считаем отказы подряд;
open       — после threshold отказов подряд запросы к домену не отправляются cooldown секунд;
half-open  — после паузы пропускается ОДИН пробный запрос: успех → closed, отказ → снова open.
"""
import time


class DomainBreaker:
    def __init__(self, threshold: int = 3, cooldown: float = 20.0, on_change=None):
        self.threshold = threshold
        self.cooldown = cooldown
        self.on_change = on_change or (lambda host, old, new, info: None)
        self.state: dict[str, str] = {}
        self.fails: dict[str, int] = {}
        self.opened_at: dict[str, float] = {}
        self.probe_in_flight: dict[str, bool] = {}
        self.activations: dict[str, int] = {}
        self.skipped: dict[str, int] = {}

    def _set(self, host, new, **info):
        old = self.state.get(host, "closed")
        if old != new:
            self.state[host] = new
            self.on_change(host, old, new, info)

    def allow(self, host: str) -> bool:
        st = self.state.get(host, "closed")
        if st == "closed":
            return True
        if st == "open":
            if time.monotonic() - self.opened_at[host] >= self.cooldown:
                self._set(host, "half-open")
                self.probe_in_flight[host] = True
                return True                         # пробный запрос
            self.skipped[host] = self.skipped.get(host, 0) + 1
            return False
        # half-open: пока пробный запрос не завершён — остальных не пускаем
        if self.probe_in_flight.get(host):
            self.skipped[host] = self.skipped.get(host, 0) + 1
            return False
        self.probe_in_flight[host] = True
        return True

    def record_success(self, host: str):
        self.fails[host] = 0
        self.probe_in_flight[host] = False
        self._set(host, "closed")

    def record_failure(self, host: str):
        self.probe_in_flight[host] = False
        st = self.state.get(host, "closed")
        self.fails[host] = self.fails.get(host, 0) + 1
        if st == "half-open" or self.fails[host] >= self.threshold:
            self.opened_at[host] = time.monotonic()
            self.activations[host] = self.activations.get(host, 0) + 1
            self._set(host, "open", consecutive_failures=self.fails[host])

    def remaining_cooldown(self, host: str) -> float:
        if self.state.get(host) != "open":
            return 0.0
        return max(0.0, self.cooldown - (time.monotonic() - self.opened_at[host]))

    @property
    def total_activations(self) -> int:
        return sum(self.activations.values())
