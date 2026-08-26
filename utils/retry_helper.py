"""
utils/retry_helper.py

A small, dependency-free retry decorator with exponential backoff + jitter.
Understands HTTP-flavoured errors (429 throttling, 5xx, timeouts, connection
drops) and respects a Retry-After header when the wrapped function raises
a RetryableHTTPError carrying one.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger("bc_sync")

T = TypeVar("T")


class RetryableHTTPError(Exception):
    """Raised by callers to signal a retryable HTTP failure (429/5xx/timeout)."""

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class NonRetryableError(Exception):
    """Raised by callers to signal a failure that should NOT be retried
    (e.g. 400 Bad Request, 401 invalid credentials after refresh, 404)."""


def retry_with_backoff(
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple = (RetryableHTTPError, ConnectionError, TimeoutError),
):
    """
    Decorator factory. Retries the wrapped function on the given exception
    types using exponential backoff with jitter. If the raised exception is a
    RetryableHTTPError with a `retry_after` value (e.g. from BC's 429
    Retry-After header), that value takes priority over the computed backoff.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt == max_attempts:
                        break

                    retry_after = getattr(exc, "retry_after", None)
                    if retry_after:
                        delay = float(retry_after)
                    else:
                        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                        delay += random.uniform(0, delay * 0.25)  # jitter

                    logger.warning(
                        f"{func.__name__} failed (attempt {attempt}/{max_attempts}): "
                        f"{exc}. Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)

            logger.error(f"{func.__name__} failed after {max_attempts} attempts: {last_exc}")
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
