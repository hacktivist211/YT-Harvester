"""Jittered exponential backoff and retry semantics."""

from __future__ import annotations

import random

import pytest

from ytharvest.net.ratelimit import RateLimiter, compute_delays


def make_limiter(**kwargs):
    sleeps: list[float] = []
    limiter = RateLimiter(
        base=1.0,
        factor=2.0,
        max_delay=60.0,
        jitter=(1.0, 4.0),
        rng=random.Random(1234),
        sleep=sleeps.append,
        **kwargs,
    )
    return limiter, sleeps


def test_backoff_is_exponential_and_capped():
    limiter, _ = make_limiter()
    raw = [limiter.backoff(n) for n in range(8)]
    assert raw == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_jitter_stays_inside_the_configured_window():
    limiter, _ = make_limiter()
    for attempt in range(20):
        delay = limiter.next_delay(attempt)
        base = limiter.backoff(attempt)
        extra = delay - base
        assert 1.0 <= extra <= 4.0


def test_delays_are_randomised_not_monotonic():
    """Two identical attempts must not produce identical delays."""
    limiter, _ = make_limiter()
    values = {limiter.next_delay(0) for _ in range(50)}
    assert len(values) > 5


def test_total_sleep_is_tracked():
    limiter, sleeps = make_limiter()
    limiter.sleep_for(0, key="media")
    limiter.sleep_for(1, key="media")
    assert len(sleeps) == 2
    assert limiter.stats()["attempts"]["media"] == 2
    assert limiter.stats()["total_slept"] == pytest.approx(sum(sleeps), abs=0.01)


def test_execute_retries_transient_then_succeeds():
    limiter, sleeps = make_limiter()
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return "ok"

    assert limiter.execute(flaky, key="job") == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2                     # slept between attempts
    assert limiter.stats()["attempts"]["job"] == 2


def test_execute_gives_up_after_max_retries():
    limiter, sleeps = make_limiter()
    limiter.max_retries = 2

    def always_429() -> None:
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    with pytest.raises(RuntimeError):
        limiter.execute(always_429)
    assert len(sleeps) == 2                     # one sleep per retry, none after the last


def test_execute_does_not_retry_fatal_errors():
    limiter, sleeps = make_limiter()

    def fatal() -> None:
        raise ValueError("unsupported format")

    with pytest.raises(ValueError):
        limiter.execute(fatal)
    assert sleeps == []


def test_custom_retry_predicate():
    limiter, sleeps = make_limiter()

    def boom() -> None:
        raise KeyError("weird")

    with pytest.raises(KeyError):
        limiter.execute(boom, retry_when=lambda exc: False)
    assert sleeps == []


def test_non_transient_marker_check():
    assert RateLimiter.is_transient("HTTP Error 403: Forbidden")
    assert RateLimiter.is_transient("Connection reset by peer")
    assert not RateLimiter.is_transient("Private video")


def test_invalid_jitter_window_rejected():
    with pytest.raises(ValueError):
        RateLimiter(jitter=(5.0, 1.0))


def test_compute_delays_helper_is_deterministic_with_seed():
    first = compute_delays(range(5), rng=random.Random(7))
    second = compute_delays(range(5), rng=random.Random(7))
    assert first == second
    assert len(first) == 5
