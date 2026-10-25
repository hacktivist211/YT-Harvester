"""Jittered exponential backoff + request pacing.

Delay model::

    t_delay(n) = min(base * factor**n, max_delay) + random(jitter_low, jitter_high)

The jitter is uniformly distributed inside the configured window so that batch
jobs never fall into lock-step with YouTube's rate limiter.
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, TypeVar

from ..errors import RateLimitedError

T = TypeVar("T")

TRANSIENT_MARKERS = (
    "429",
    "403",
    "rate-limit",
    "too many requests",
    "http error 5",
    "timed out",
    "connection reset",
    "temporarily unavailable",
    "video unavailable",
)


class RateLimiter:
    """Thread-safe backoff calculator + retry executor."""

    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        max_delay: float = 60.0,
        jitter: tuple[float, float] = (1.0, 4.0),
        max_retries: int = 5,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if jitter[0] > jitter[1]:
            raise ValueError("jitter window must be (low, high)")
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.max_retries = max_retries
        self.rng = rng or random.Random()
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self.attempts: dict[str, int] = {}
        self.last_delay: float = 0.0
        self.total_slept: float = 0.0

    # ------------------------------------------------------------ delays
    def backoff(self, attempt: int) -> float:
        """Deterministic exponential component for ``attempt`` (0-based)."""
        return min(self.base * (self.factor ** attempt), self.max_delay)

    def jitter_for(self, attempt: int) -> float:
        low, high = self.jitter
        return self.rng.uniform(low, high)

    def next_delay(self, attempt: int) -> float:
        delay = self.backoff(attempt) + self.jitter_for(attempt)
        self.last_delay = delay
        return delay

    def sleep_for(self, attempt: int, key: str = "") -> float:
        delay = self.next_delay(attempt)
        with self._lock:
            self.total_slept += delay
            if key:
                self.attempts[key] = self.attempts.get(key, 0) + 1
        self._sleep(delay)
        return delay

    # ------------------------------------------------------------- retry
    @staticmethod
    def is_transient(message: str) -> bool:
        lowered = message.lower()
        return any(marker in lowered for marker in TRANSIENT_MARKERS)

    def execute(
        self,
        fn: Callable[..., T],
        *args: object,
        key: str = "",
        retry_when: Callable[[Exception], bool] | None = None,
        on_retry: Callable[[int, Exception, float], None] | None = None,
        **kwargs: object,
    ) -> T:
        """Run ``fn`` with jittered exponential backoff on transient failures."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below if fatal
                last_exc = exc
                transient = retry_when(exc) if retry_when else self.is_transient(str(exc))
                if not transient or attempt >= self.max_retries:
                    raise
                delay = self.next_delay(attempt)
                if on_retry:
                    on_retry(attempt, exc, delay)
                with self._lock:
                    self.total_slept += delay
                    self.attempts[key or getattr(fn, "__name__", "fn")] = (
                        self.attempts.get(key or getattr(fn, "__name__", "fn"), 0) + 1
                    )
                self._sleep(delay)
        raise RateLimitedError(0, str(last_exc))  # pragma: no cover - unreachable

    # ---------------------------------------------------------- pacing
    @contextmanager
    def paced(self, key: str = "") -> Iterator[None]:
        """Context manager adding a randomised inter-request gap."""
        delay = self.jitter_for(0) * 0.25
        if delay > 0:
            with self._lock:
                self.total_slept += delay
            self._sleep(delay)
        try:
            yield
        finally:
            if key:
                with self._lock:
                    self.attempts[key] = self.attempts.get(key, 0)

    def pause(self, seconds: float) -> None:
        """Sleep a pre-computed delay (used by the gatekeeper after a 429)."""
        if seconds <= 0:
            return
        with self._lock:
            self.total_slept += seconds
            self.last_delay = seconds
        self._sleep(seconds)

    def wait_until(self, timestamp: float) -> None:
        """Sleep until an absolute (monotonic) time - used for token refresh."""
        now = self._clock()
        if timestamp > now:
            self._sleep(timestamp - now)

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "attempts": dict(self.attempts),
                "total_slept": round(self.total_slept, 3),
                "last_delay": round(self.last_delay, 3),
            }


def compute_delays(attempts: Iterable[int], base: float = 1.0, factor: float = 2.0,
                   max_delay: float = 60.0, jitter: tuple[float, float] = (1.0, 4.0),
                   rng: random.Random | None = None) -> list[float]:
    """Helper used by tests/CLI to preview a backoff schedule."""
    limiter = RateLimiter(base, factor, max_delay, jitter, rng=rng)
    return [limiter.next_delay(a) for a in attempts]
