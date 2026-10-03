"""Retry policies (tenacity)."""
from __future__ import annotations

from typing import Any, Callable

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from avd.config import get_settings


def _build_retry_config() -> tuple[int, float, float]:
    s = get_settings()
    return s.retry_max_attempts, s.retry_initial_wait_s, s.retry_max_wait_s


def with_retry(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: bounded retries with exponential jitter."""
    max_attempts, initial, max_wait = _build_retry_config()
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=initial, max=max_wait),
        retry=retry_if_exception_type((TimeoutError, ConnectionError, OSError)),
        reraise=True,
    )(func)
