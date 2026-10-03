"""Per-host circuit breaker — minimal hand-rolled implementation."""
from __future__ import annotations

import time
from urllib.parse import urlparse


class _Breaker:
    """Minimal per-host circuit breaker.

    State machine:
      - CLOSED (default): requests flow
      - OPEN: requests short-circuit for `reset_timeout_s` seconds
      - After timeout: re-allow one request (half-open); if success → CLOSED, if fail → OPEN
    """

    __slots__ = ("fail_max", "reset_timeout_s", "_fail_count", "_opened_at", "_state")

    def __init__(self, fail_max: int, reset_timeout_s: int) -> None:
        self.fail_max = fail_max
        self.reset_timeout_s = reset_timeout_s
        self._fail_count = 0
        self._opened_at: float = 0.0
        self._state = "closed"  # closed | open | half_open

    @property
    def is_open(self) -> bool:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.reset_timeout_s:
                self._state = "half_open"
                return False
            return True
        return False

    def record_success(self) -> None:
        if self._state == "half_open":
            self._state = "closed"
        self._fail_count = 0

    def record_failure(self) -> None:
        self._fail_count += 1
        if self._state == "half_open" or self._fail_count >= self.fail_max:
            self._state = "open"
            self._opened_at = time.monotonic()


_breakers: dict[str, _Breaker] = {}


def _get_breaker(host: str) -> _Breaker:
    if host not in _breakers:
        from avd.config import get_settings

        s = get_settings()
        _breakers[host] = _Breaker(
            fail_max=s.breaker_fail_max,
            reset_timeout_s=s.breaker_reset_timeout_s,
        )
    return _breakers[host]


def is_open(host: str) -> bool:
    return _get_breaker(host).is_open


def record_success(host: str) -> None:
    try:
        _get_breaker(host).record_success()
    except Exception:
        pass


def record_failure(host: str) -> None:
    try:
        _get_breaker(host).record_failure()
    except Exception:
        pass


def host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or "unknown"
    except Exception:
        return "unknown"


def reset(host: str | None = None) -> None:
    """Reset breakers (one host or all)."""
    global _breakers
    if host:
        _breakers.pop(host, None)
    else:
        _breakers.clear()
