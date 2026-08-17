"""A single, shared, fully-bounded retry policy for transient API request
failures -- used by `api_client.ApiClient` for both source API page
requests and OAuth token-endpoint requests (see `oauth.py`), so there is
exactly one retry implementation in this connector, not two.

Design goals, all load-bearing:
  * Deterministic and observable under tests: every input the delay
    calculation depends on (attempt number, base delay, cap, a random
    source, a monotonic clock) is a plain injectable value/callable --
    nothing here reaches for `time.sleep`/`random.random`/`time.monotonic`
    directly at call sites that need to be test-controlled. Production
    code paths default to the real functions.
  * Bounded by both attempts *and* elapsed wall time
    (`RetryBudget.max_attempts`/`max_elapsed_seconds`), using a monotonic
    clock for every elapsed-time decision -- wall-clock time can jump
    (NTP adjustment, DST, a suspended laptop); a retry budget must not.
  * A computed delay (backoff or `Retry-After`) is never allowed to sleep
    past the remaining elapsed-time budget -- see `remaining_budget`/the
    "never exceed the deadline" contract in `ApiClient._request_with_retries`.
  * `parse_retry_after` accepts both documented `Retry-After` forms (a
    non-negative number of seconds, or an HTTP-date -- RFC 9110 SS10.2.3)
    and returns `None` (never raises) for anything malformed, negative,
    non-finite, or unreasonably large -- callers fall back to exponential
    backoff in that case, exactly as if no `Retry-After` header had been
    sent at all.
"""
from __future__ import annotations

import math
import random as _random_module
import time as _time_module
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

# HTTP statuses this connector retries. Deliberately excludes 500 (not
# requested by the source contract -- an unconditional retry of every 5xx
# would mask real, non-transient server bugs) and every 4xx except the
# separate, single-shot OAuth 401-refresh-and-replay handled in
# `api_client.ApiClient` (never counted against this retry budget).
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 502, 503, 504})

# A `Retry-After` value (seconds, or the delay computed from an HTTP-date)
# larger than this is treated as unreasonable/malformed and ignored in
# favor of exponential backoff -- a hostile or badly misconfigured server
# must never be able to force an arbitrarily long single sleep just by
# sending a huge Retry-After value.
MAX_REASONABLE_RETRY_AFTER_SECONDS = 3600.0  # 1 hour


def parse_retry_after(value: str | None, *, now: Callable[[], datetime] | None = None) -> float | None:
    """Parse a `Retry-After` header value into a non-negative delay in
    seconds, or return `None` if it is missing, malformed, negative,
    non-finite, or unreasonably large (the caller should fall back to
    exponential backoff in every `None` case -- this function never
    raises).

    Supports both RFC 9110-documented forms:
      * A non-negative integer/float number of seconds.
      * An HTTP-date (RFC 5322/1123 style, e.g.
        "Wed, 21 Octə2026 07:28:00 GMT") -- the delay is computed
        relative to `now()` (defaults to the real UTC wall clock, since
        HTTP-date is inherently a wall-clock concept, unlike the
        monotonic elapsed-time budget this module also tracks). A past
        date yields a delay of `0.0` (retry immediately), never a
        negative number.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    try:
        seconds = float(text)
    except ValueError:
        seconds = None

    if seconds is not None:
        if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_REASONABLE_RETRY_AFTER_SECONDS:
            return None
        return seconds

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    current = now() if now is not None else datetime.now(timezone.utc)
    delta = (parsed - current).total_seconds()
    if not math.isfinite(delta):
        return None
    if delta <= 0:
        return 0.0
    if delta > MAX_REASONABLE_RETRY_AFTER_SECONDS:
        return None
    return delta


def compute_backoff(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    random_fn: Callable[[], float] = _random_module.random,
) -> float:
    """Exponential backoff with "full jitter" (see AWS's well-known
    Exponential Backoff and Jitter architecture note): the exponential
    schedule (`base_delay * 2**attempt`, capped at `max_delay`) is the
    *ceiling* of a uniform random draw, not the delay itself -- this
    spreads out retries from many concurrent callers far better than
    additive jitter while still being trivially deterministic under test
    (inject `random_fn` returning a fixed value, e.g. `lambda: 1.0` for
    the ceiling or `lambda: 0.0` for zero).

    `attempt` is 0-indexed: 0 is the delay before the *first* retry.
    `random_fn()` must return a value in `[0.0, 1.0]`; the result is
    always in `[0.0, max_delay]`.
    """
    ceiling = min(max_delay, base_delay * (2**attempt))
    draw = random_fn()
    draw = 0.0 if draw < 0 else (1.0 if draw > 1 else draw)
    return draw * ceiling


class RetryBudget:
    """Tracks one logical request's retry attempts and elapsed time
    against a fixed budget, using an injectable monotonic clock (defaults
    to `time.monotonic`, never wall-clock time) so tests can drive the
    budget deterministically without a real sleep."""

    def __init__(
        self,
        *,
        max_attempts: int,
        max_elapsed_seconds: float,
        clock: Callable[[], float] = _time_module.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.max_elapsed_seconds = max_elapsed_seconds
        self._clock = clock
        self._start = clock()
        self.attempt_number = 0  # number of requests already sent (1 after the first attempt)

    def elapsed(self) -> float:
        return max(0.0, self._clock() - self._start)

    def remaining(self) -> float:
        """Seconds left in the elapsed-time budget -- never negative."""
        return max(0.0, self.max_elapsed_seconds - self.elapsed())

    def record_attempt(self) -> None:
        self.attempt_number += 1

    def attempts_exhausted(self) -> bool:
        return self.attempt_number >= self.max_attempts

    def duration_exhausted(self) -> bool:
        return self.elapsed() >= self.max_elapsed_seconds

    def exhausted(self) -> bool:
        return self.attempts_exhausted() or self.duration_exhausted()


class RetryBudgetExhausted(Exception):
    """Raised internally by `BoundedRetryExecutor.run` when neither the
    attempt limit nor the elapsed-time budget allow another attempt, and
    the last failure was a retryable HTTP status (not an exception -- an
    exhausted retryable *exception* is re-raised as itself instead, so
    its original type/message is preserved for the caller). Callers
    translate this into their own domain error (e.g.
    `api_client.DownloadError`, `oauth.OAuthError`)."""

    def __init__(self, reason: str, status_code: int | None) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"retry budget exhausted (last: {reason})")


class BoundedRetryExecutor:
    """Drives one logical HTTP request through the one shared, bounded
    retry policy this connector uses for every retryable API call --
    source page requests and OAuth token-endpoint requests alike (see
    `api_client.ApiClient`/`oauth.OAuthTokenManager`). Retries only:
      * the caller-supplied retryable exception types (connection
        establishment failures, resets, connect/read timeouts), and
      * responses the caller's `is_retryable_response` predicate accepts
        (this connector only ever passes HTTP 429/502/503/504 -- see
        `RETRYABLE_STATUS`).
    Stops -- and re-raises the last failure -- as soon as either the
    attempt limit or the elapsed-time budget (whichever comes first) is
    reached. A computed delay (from `Retry-After` or backoff) that would
    exceed the *remaining* elapsed-time budget is never slept; the
    request fails immediately instead (see module docstring) -- this
    keeps "never sleep beyond the remaining retry budget" an exact
    guarantee rather than a best effort.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        max_retry_duration_seconds: float,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        clock: Callable[[], float] = _time_module.monotonic,
        random_fn: Callable[[], float] = _random_module.random,
        sleep_fn: Callable[[float], None] = _time_module.sleep,
        on_retry: Callable[..., None] | None = None,
    ) -> None:
        self._max_attempts = max_attempts
        self._max_retry_duration_seconds = max_retry_duration_seconds
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._clock = clock
        self._random_fn = random_fn
        self._sleep_fn = sleep_fn
        self._on_retry = on_retry

    def run(
        self,
        attempt_fn: Callable[[], object],
        *,
        retryable_exceptions: tuple[type[BaseException], ...],
        is_retryable_response: Callable[[object], bool],
        get_retry_after: Callable[[object], str | None],
        close_response: Callable[[object], None],
    ):
        """`attempt_fn()` performs exactly one HTTP attempt and returns a
        response object (of whatever type the caller uses) or raises.
        Returns the first response that `is_retryable_response` rejects
        (a definitive success or a non-retryable failure status -- the
        caller interprets it further) or re-raises once the retry budget
        is exhausted."""
        budget = RetryBudget(
            max_attempts=self._max_attempts, max_elapsed_seconds=self._max_retry_duration_seconds, clock=self._clock
        )
        while True:
            budget.record_attempt()
            last_exc: BaseException | None = None
            status_code: int | None = None
            retry_after_header: str | None = None
            reason = ""

            try:
                response = attempt_fn()
            except retryable_exceptions as exc:
                last_exc = exc
                reason = exc.__class__.__name__
            else:
                if not is_retryable_response(response):
                    return response
                status_code = getattr(response, "status_code", None)
                retry_after_header = get_retry_after(response)
                reason = f"http_{status_code}"
                close_response(response)  # release the failed response before waiting/retrying

            if budget.attempts_exhausted() or budget.duration_exhausted():
                if last_exc is not None:
                    raise last_exc
                raise RetryBudgetExhausted(reason, status_code)

            delay: float | None = None
            if retry_after_header is not None:
                delay = parse_retry_after(retry_after_header)
            if delay is None:
                delay = compute_backoff(
                    budget.attempt_number - 1,
                    base_delay=self._base_delay_seconds,
                    max_delay=self._max_delay_seconds,
                    random_fn=self._random_fn,
                )
            delay = max(0.0, min(delay, self._max_delay_seconds))

            if delay > budget.remaining():
                # A valid Retry-After (or the backoff schedule) would
                # itself blow through the elapsed-time deadline -- fail
                # now rather than sleep a silently-truncated duration
                # that the server never actually asked for.
                if last_exc is not None:
                    raise last_exc
                raise RetryBudgetExhausted(reason, status_code)

            if self._on_retry is not None:
                self._on_retry(
                    attempt_number=budget.attempt_number,
                    delay_seconds=delay,
                    reason=reason,
                    status_code=status_code,
                    elapsed_seconds=budget.elapsed(),
                )
            self._sleep_fn(delay)
