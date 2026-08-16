"""Standard-library unit tests for tuva_ingest.retry: the shared bounded
retry policy (parse_retry_after, compute_backoff, RetryBudget,
BoundedRetryExecutor) used by both api_client.py and oauth.py.

Every test uses a fake/injected clock, random source, and sleep function
-- never `time.sleep`/`random.random`/`time.monotonic` for real -- so
this whole file runs in well under a second and is fully deterministic.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.retry import (  # noqa: E402
    RETRYABLE_STATUS,
    BoundedRetryExecutor,
    RetryBudget,
    RetryBudgetExhausted,
    compute_backoff,
    parse_retry_after,
)


class _FakeClock:
    """A controllable monotonic clock: starts at 0.0, advances only when
    `advance()` is called (e.g. from a fake sleep_fn) -- so a test can
    assert exactly how much simulated time elapsed without ever sleeping
    for real."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestRetryableStatus(unittest.TestCase):
    def test_only_429_502_503_504_are_retryable(self):
        self.assertEqual(RETRYABLE_STATUS, frozenset({429, 502, 503, 504}))

    def test_500_is_not_retryable(self):
        self.assertNotIn(500, RETRYABLE_STATUS)


class TestParseRetryAfter(unittest.TestCase):
    def test_numeric_seconds(self):
        self.assertEqual(parse_retry_after("5"), 5.0)
        self.assertEqual(parse_retry_after("0"), 0.0)
        self.assertEqual(parse_retry_after("2.5"), 2.5)

    def test_negative_is_rejected(self):
        self.assertIsNone(parse_retry_after("-1"))

    def test_non_finite_is_rejected(self):
        self.assertIsNone(parse_retry_after("inf"))
        self.assertIsNone(parse_retry_after("nan"))

    def test_unreasonably_large_is_rejected(self):
        self.assertIsNone(parse_retry_after("999999999"))

    def test_malformed_falls_back_to_none(self):
        self.assertIsNone(parse_retry_after("not-a-number-or-date"))

    def test_none_and_empty_are_none(self):
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after(""))
        self.assertIsNone(parse_retry_after("   "))

    def test_http_date_in_the_future(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        future = now + timedelta(seconds=30)
        header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        delay = parse_retry_after(header, now=lambda: now)
        self.assertAlmostEqual(delay, 30.0, delta=1.0)

    def test_http_date_in_the_past_yields_zero(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        past = now - timedelta(seconds=30)
        header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        delay = parse_retry_after(header, now=lambda: now)
        self.assertEqual(delay, 0.0)

    def test_http_date_malformed_is_none(self):
        self.assertIsNone(parse_retry_after("not, a real HTTP-date"))

    def test_http_date_too_far_in_future_is_rejected(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        far_future = now + timedelta(days=365)
        header = far_future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertIsNone(parse_retry_after(header, now=lambda: now))


class TestComputeBackoff(unittest.TestCase):
    def test_zero_jitter_draw_yields_zero_delay(self):
        delay = compute_backoff(0, base_delay=1.0, max_delay=30.0, random_fn=lambda: 0.0)
        self.assertEqual(delay, 0.0)

    def test_max_jitter_draw_yields_the_full_exponential_ceiling(self):
        delay = compute_backoff(2, base_delay=1.0, max_delay=30.0, random_fn=lambda: 1.0)
        self.assertEqual(delay, 4.0)  # min(30, 1 * 2**2)

    def test_capped_at_max_delay(self):
        delay = compute_backoff(10, base_delay=1.0, max_delay=5.0, random_fn=lambda: 1.0)
        self.assertEqual(delay, 5.0)

    def test_deterministic_for_a_fixed_random_fn(self):
        d1 = compute_backoff(3, base_delay=2.0, max_delay=60.0, random_fn=lambda: 0.5)
        d2 = compute_backoff(3, base_delay=2.0, max_delay=60.0, random_fn=lambda: 0.5)
        self.assertEqual(d1, d2)


class TestRetryBudget(unittest.TestCase):
    def test_attempts_exhausted(self):
        clock = _FakeClock()
        budget = RetryBudget(max_attempts=2, max_elapsed_seconds=1000.0, clock=clock)
        self.assertFalse(budget.attempts_exhausted())
        budget.record_attempt()
        self.assertFalse(budget.attempts_exhausted())
        budget.record_attempt()
        self.assertTrue(budget.attempts_exhausted())

    def test_duration_exhausted(self):
        clock = _FakeClock()
        budget = RetryBudget(max_attempts=1000, max_elapsed_seconds=10.0, clock=clock)
        self.assertFalse(budget.duration_exhausted())
        clock.advance(11.0)
        self.assertTrue(budget.duration_exhausted())

    def test_remaining_never_negative(self):
        clock = _FakeClock()
        budget = RetryBudget(max_attempts=10, max_elapsed_seconds=5.0, clock=clock)
        clock.advance(100.0)
        self.assertEqual(budget.remaining(), 0.0)


class _FakeResponse:
    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self._retry_after = retry_after
        self.closed = False

    def headers_get(self):
        return self._retry_after


class TestBoundedRetryExecutor(unittest.TestCase):
    def _executor(self, *, max_attempts=5, max_retry_duration_seconds=1000.0, clock=None, sleeps=None):
        clock = clock or _FakeClock()
        sleeps = sleeps if sleeps is not None else []

        def sleep_fn(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        return BoundedRetryExecutor(
            max_attempts=max_attempts,
            max_retry_duration_seconds=max_retry_duration_seconds,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
            clock=clock,
            random_fn=lambda: 0.0,  # deterministic: always the minimum backoff
            sleep_fn=sleep_fn,
        ), sleeps, clock

    @staticmethod
    def _close(response):
        response.closed = True

    def test_retries_retryable_exception_then_succeeds(self):
        executor, sleeps, _ = self._executor()
        attempts = {"n": 0}

        def attempt():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("boom")
            return _FakeResponse(200)

        result = executor.run(
            attempt, retryable_exceptions=(ConnectionError,),
            is_retryable_response=lambda r: False,
            get_retry_after=lambda r: None, close_response=self._close,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_retries_retryable_response_then_succeeds(self):
        executor, sleeps, _ = self._executor()
        responses = [_FakeResponse(503), _FakeResponse(503), _FakeResponse(200)]

        def attempt():
            return responses.pop(0)

        result = executor.run(
            attempt, retryable_exceptions=(), is_retryable_response=lambda r: r.status_code in (503,),
            get_retry_after=lambda r: r.headers_get(), close_response=self._close,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(sleeps), 2)

    def test_non_retryable_response_returned_immediately_no_sleep(self):
        executor, sleeps, _ = self._executor()
        response = _FakeResponse(404)
        result = executor.run(
            lambda: response, retryable_exceptions=(), is_retryable_response=lambda r: False,
            get_retry_after=lambda r: None, close_response=self._close,
        )
        self.assertEqual(result.status_code, 404)
        self.assertEqual(sleeps, [])
        self.assertFalse(response.closed)  # never "closed early" by the retry loop for a terminal response

    def test_failed_response_is_closed_before_sleeping(self):
        order = []

        def sleep_fn(seconds):
            order.append(("sleep", seconds))

        clock = _FakeClock()
        executor = BoundedRetryExecutor(
            max_attempts=3, max_retry_duration_seconds=1000.0, clock=clock,
            random_fn=lambda: 0.0, sleep_fn=sleep_fn,
        )
        responses = [_FakeResponse(503), _FakeResponse(200)]

        def close(response):
            order.append(("close", response.status_code))
            response.closed = True

        def attempt():
            return responses.pop(0)

        executor.run(
            attempt, retryable_exceptions=(), is_retryable_response=lambda r: r.status_code == 503,
            get_retry_after=lambda r: None, close_response=close,
        )
        self.assertEqual(order[0], ("close", 503))
        self.assertEqual(order[1][0], "sleep")

    def test_exhausted_by_attempt_count_raises(self):
        executor, sleeps, _ = self._executor(max_attempts=3)

        def attempt():
            return _FakeResponse(503)

        with self.assertRaises(RetryBudgetExhausted) as ctx:
            executor.run(
                attempt, retryable_exceptions=(), is_retryable_response=lambda r: True,
                get_retry_after=lambda r: None, close_response=self._close,
            )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(len(sleeps), 2)  # 3 attempts, 2 sleeps between them

    def test_exhausted_by_elapsed_duration_raises(self):
        # random_fn=1.0 (max jitter draw) so each backoff delay actually
        # advances the fake clock -- with random_fn=0.0 (the other
        # tests' default) every delay would be zero and duration could
        # never become the limiting factor, only attempts would.
        clock = _FakeClock()
        sleeps = []
        executor = BoundedRetryExecutor(
            max_attempts=1000, max_retry_duration_seconds=5.0, base_delay_seconds=1.0, max_delay_seconds=30.0,
            clock=clock, random_fn=lambda: 1.0, sleep_fn=lambda s: (sleeps.append(s), clock.advance(s)),
        )

        def attempt():
            return _FakeResponse(503)

        with self.assertRaises(RetryBudgetExhausted):
            executor.run(
                attempt, retryable_exceptions=(), is_retryable_response=lambda r: True,
                get_retry_after=lambda r: None, close_response=self._close,
            )
        # Duration budget (5s) was hit well before anywhere near 1000
        # attempts -- proves duration, not the attempt count, was the
        # limiting factor.
        self.assertLess(len(sleeps), 10)
        self.assertLessEqual(sum(sleeps), 5.0)

    def test_last_exception_reraised_on_exhaustion_not_a_generic_error(self):
        executor, _, _ = self._executor(max_attempts=2)

        def attempt():
            raise TimeoutError("connect timeout")

        with self.assertRaises(TimeoutError):
            executor.run(
                attempt, retryable_exceptions=(TimeoutError,), is_retryable_response=lambda r: False,
                get_retry_after=lambda r: None, close_response=self._close,
            )

    def test_numeric_retry_after_is_honored_over_backoff(self):
        clock = _FakeClock()
        sleeps = []
        executor, _, _ = self._executor(clock=clock, sleeps=sleeps)
        responses = [_FakeResponse(429, retry_after="7"), _FakeResponse(200)]

        def attempt():
            return responses.pop(0)

        executor.run(
            attempt, retryable_exceptions=(), is_retryable_response=lambda r: r.status_code == 429,
            get_retry_after=lambda r: r.headers_get(), close_response=self._close,
        )
        self.assertEqual(sleeps, [7.0])

    def test_malformed_retry_after_falls_back_to_backoff(self):
        clock = _FakeClock()
        sleeps = []
        executor, _, _ = self._executor(clock=clock, sleeps=sleeps)
        responses = [_FakeResponse(429, retry_after="garbage"), _FakeResponse(200)]

        def attempt():
            return responses.pop(0)

        executor.run(
            attempt, retryable_exceptions=(), is_retryable_response=lambda r: r.status_code == 429,
            get_retry_after=lambda r: r.headers_get(), close_response=self._close,
        )
        # random_fn=0.0 -> backoff delay is 0.0, not the malformed value
        self.assertEqual(sleeps, [0.0])

    def test_retry_after_capped_by_max_delay(self):
        clock = _FakeClock()
        sleeps = []
        executor = BoundedRetryExecutor(
            max_attempts=5, max_retry_duration_seconds=1000.0, max_delay_seconds=10.0,
            clock=clock, random_fn=lambda: 0.0, sleep_fn=lambda s: (sleeps.append(s), clock.advance(s)),
        )
        responses = [_FakeResponse(429, retry_after="500"), _FakeResponse(200)]

        def attempt():
            return responses.pop(0)

        executor.run(
            attempt, retryable_exceptions=(), is_retryable_response=lambda r: r.status_code == 429,
            get_retry_after=lambda r: r.headers_get(), close_response=self._close,
        )
        self.assertEqual(sleeps, [10.0])  # capped, not 500

    def test_retry_after_that_would_exceed_deadline_fails_immediately_never_oversleeps(self):
        clock = _FakeClock()
        sleeps = []
        executor = BoundedRetryExecutor(
            max_attempts=100, max_retry_duration_seconds=5.0, max_delay_seconds=30.0,
            clock=clock, random_fn=lambda: 0.0, sleep_fn=lambda s: (sleeps.append(s), clock.advance(s)),
        )

        def attempt():
            # A valid Retry-After (10s) that is itself larger than the
            # *entire* elapsed-time budget (5s) -- must fail immediately,
            # on the very first retry decision, with zero sleeps.
            return _FakeResponse(429, retry_after="10")

        with self.assertRaises(RetryBudgetExhausted):
            executor.run(
                attempt, retryable_exceptions=(), is_retryable_response=lambda r: True,
                get_retry_after=lambda r: r.headers_get(), close_response=self._close,
            )
        self.assertEqual(sleeps, [])
        self.assertEqual(clock.now, 0.0)

    def test_never_sleeps_longer_than_remaining_budget_across_many_retries(self):
        clock = _FakeClock()
        sleeps = []
        executor = BoundedRetryExecutor(
            max_attempts=1000, max_retry_duration_seconds=20.0, base_delay_seconds=1.0, max_delay_seconds=30.0,
            clock=clock, random_fn=lambda: 1.0, sleep_fn=lambda s: (sleeps.append(s), clock.advance(s)),
        )

        def attempt():
            return _FakeResponse(503)

        with self.assertRaises(RetryBudgetExhausted):
            executor.run(
                attempt, retryable_exceptions=(), is_retryable_response=lambda r: True,
                get_retry_after=lambda r: None, close_response=self._close,
            )
        self.assertLessEqual(sum(sleeps), 20.0)


if __name__ == "__main__":
    unittest.main()
