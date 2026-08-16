"""A production-oriented, streaming, authenticated HTTP client for both
the legacy manifest contract (see docs/API_MANIFEST.md) and the
paginated page-request contract (see docs/SOURCE_CONTRACT.md), built on
`httpx` (a reusable, explicitly-timed-out `httpx.Client`) with bounded
retries driven by the shared policy in `retry.py`.

Design goals, all load-bearing for the "authenticated API client"
capability:
  * Bearer-token auth on every request; the token is only ever placed in
    the Authorization header of the underlying request -- it is never
    interpolated into a log line or an exception message. The token
    comes from either a static credential (see `secrets.py`) or, when an
    `oauth_manager=` (`oauth.OAuthTokenManager`) is supplied, a
    proactively-refreshed OAuth access token obtained fresh before every
    request.
  * Explicit connect/read/write/pool timeouts (`IngestConfig.httpx_timeout()`),
    bounded retries with exponential backoff + jitter, honoring
    `Retry-After` (both the numeric-seconds and HTTP-date forms -- see
    `retry.parse_retry_after`) on 429/502/503/504, bounded by both a
    maximum attempt count *and* an absolute elapsed-time budget
    (`max_retry_duration_seconds`) -- never an unbounded sleep or an
    unbounded retry loop. Ordinary 4xx (400/403/404/409/422), envelope
    validation failures, and checksum failures are never retried; HTTP
    500 is deliberately *not* in the retryable set (only 429/502/503/504
    are, per this connector's own retry policy -- see `retry.RETRYABLE_STATUS`).
  * A single, dedicated exception to the "never retry 401" rule: when an
    `oauth_manager` is configured and a request returns HTTP 401, this
    client permits exactly one forced token refresh and one replay of
    that same request -- authentication recovery, not a general retry.
    If the replay also returns 401, it is returned to the caller as a
    terminal failure; no further refresh or replay is attempted, and
    this path never counts against (or is confused with) the bounded
    retry budget above.
  * Redirects are never followed automatically (`follow_redirects=False`)
    -- a request redirecting to an unexpected host must never silently
    receive this client's bearer token.
  * Downloads stream to a temporary `.part` file (never buffered fully in
    memory), verifying SHA-256 and byte count incrementally so an
    oversized or corrupt response is aborted before it fills memory or
    disk unnecessarily; the `.part` file is renamed into place only after
    verification succeeds (atomic completion) and is removed on any
    failure.
  * A real `httpx.BaseTransport` (e.g. `httpx.MockTransport`) can be
    injected via `transport=` -- this is how `tests/unit/test_api_client.py`
    exercises every retry/auth/checksum path with zero real network
    access, per this repository's testing policy. `clock=`/`random_fn=`/
    `sleep_fn=` are likewise injectable so retry/backoff/duration-budget
    tests run instantly and deterministically, never sleeping for real.
"""
from __future__ import annotations

import hashlib
import json
import random as _random_module
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .errors import ChecksumError, DownloadError, ManifestError
from .logging_utils import log_event
from .manifest import Artifact
from .retry import RETRYABLE_STATUS, BoundedRetryExecutor, RetryBudgetExhausted

DEFAULT_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB hard ceiling, independent of declared size
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_MANIFEST_BYTES = 16 * 1024 * 1024  # manifests are small JSON documents

# httpx exception umbrella classes that represent transient, retry-worthy
# failures: connection establishment failures, resets, and connect/read/
# write/pool timeouts. `httpx.TimeoutException` covers Connect/Read/
# Write/PoolTimeout; `httpx.NetworkError` covers ConnectError/ReadError/
# WriteError/CloseError (DNS failures, connection resets, refused
# connections, ...). Neither includes `httpx.HTTPStatusError` (this
# client never calls `raise_for_status()`) or any 4xx/programming error
# -- those are never retried.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.NetworkError)


def user_agent() -> str:
    return f"tuva-ingest/{__version__}"


@dataclass
class DownloadResult:
    table: str
    path: Path
    sha256: str
    size_bytes: int
    duration_ms: float


class ApiClient:
    def __init__(
        self,
        *,
        token: str = "",
        oauth_manager: Any = None,
        timeout_seconds: float = 30.0,
        timeout: "httpx.Timeout | None" = None,
        max_retries: int = 5,
        max_retry_delay_seconds: float = 30.0,
        max_retry_duration_seconds: float = 60.0,
        logger: Any = None,
        run_id: str | None = None,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        clock: Callable[[], float] = time.monotonic,
        random_fn: Callable[[], float] = _random_module.random,
        sleep_fn: Any = time.sleep,
        transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        """`token` is a static bearer credential (see `secrets.py`),
        mutually exclusive in practice with `oauth_manager` (an
        `oauth.OAuthTokenManager`) -- when `oauth_manager` is given, it
        is consulted for a fresh access token before every request
        instead of `token`. Exactly one of the two should be configured
        by the caller (`cli.py`); this class does not itself enforce
        that, since a bare static-token client with no OAuth is also a
        completely valid configuration (the default, backward-compatible
        one)."""
        self._token = token
        self._oauth_manager = oauth_manager
        self._max_retries = max_retries
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._max_retry_duration_seconds = max_retry_duration_seconds
        self._logger = logger
        self._run_id = run_id
        self._max_artifact_bytes = max_artifact_bytes
        self._clock = clock
        self._random_fn = random_fn
        self._sleep_fn = sleep_fn
        resolved_timeout = timeout if timeout is not None else httpx.Timeout(timeout_seconds)
        self._client = httpx.Client(
            timeout=resolved_timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _current_token(self) -> str:
        if self._oauth_manager is not None:
            return self._oauth_manager.get_access_token()
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._current_token()}",
            "User-Agent": user_agent(),
            "Accept": "application/json, text/csv;q=0.9, */*;q=0.1",
        }

    def _log_retry(self, *, attempt_number: int, delay_seconds: float, reason: str, status_code, elapsed_seconds: float, endpoint: str | None = None) -> None:
        if self._logger is not None:
            log_event(
                self._logger, "http_retry_scheduled", run_id=self._run_id,
                attempt=attempt_number, delay_seconds=round(delay_seconds, 3), status_code=status_code,
                reason=reason, elapsed_seconds=round(elapsed_seconds, 3), endpoint=endpoint,
            )

    def _executor(self, *, endpoint_name: str | None = None) -> BoundedRetryExecutor:
        return BoundedRetryExecutor(
            max_attempts=self._max_retries + 1,
            max_retry_duration_seconds=self._max_retry_duration_seconds,
            base_delay_seconds=1.0,
            max_delay_seconds=self._max_retry_delay_seconds,
            clock=self._clock,
            random_fn=self._random_fn,
            sleep_fn=self._sleep_fn,
            on_retry=lambda **kw: self._log_retry(endpoint=endpoint_name, **kw),
        )

    def _send_once(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        request = self._client.build_request(method, url, headers=headers or self._headers(), params=params)
        return self._client.send(request, stream=True)

    def _request_with_retries(
        self, method: str, url: str, *, params: dict | None = None, endpoint_name: str | None = None
    ) -> httpx.Response:
        """Issue one logical request, transparently retrying transient
        failures (connection/timeout errors, 429/502/503/504) up to
        `max_retries` additional times, bounded jointly by the attempt
        count and `max_retry_duration_seconds` (whichever is hit first).
        Returns the streamed `httpx.Response` (status already known, body
        not yet read) for the caller to inspect/consume -- callers own
        `response.close()`. Never logs `url` if it might carry
        credentials in its query string -- only `endpoint_name` (a fixed,
        safe label) is logged.

        If `oauth_manager` is configured and the *final* response
        (whether from the first attempt or after the bounded retries
        above) is HTTP 401, this method performs exactly one forced
        token refresh and one replay -- never more, and never counted
        against the bounded retry budget itself (an expired token is an
        authentication-recovery event, not a transient server failure).
        """
        executor = self._executor(endpoint_name=endpoint_name)

        def _is_retryable_response(response: httpx.Response) -> bool:
            return response.status_code in RETRYABLE_STATUS

        def _get_retry_after(response: httpx.Response) -> str | None:
            return response.headers.get("Retry-After")

        def _close(response: httpx.Response) -> None:
            response.close()

        try:
            response = executor.run(
                lambda: self._send_once(method, url, params=params),
                retryable_exceptions=RETRYABLE_EXCEPTIONS,
                is_retryable_response=_is_retryable_response,
                get_retry_after=_get_retry_after,
                close_response=_close,
            )
        except RetryBudgetExhausted as exc:
            raise DownloadError(
                f"request to endpoint {endpoint_name or url.split('?')[0]!r} exhausted retries "
                f"(last status {exc.status_code})"
            ) from None
        except RETRYABLE_EXCEPTIONS as exc:
            raise DownloadError(
                f"request to endpoint {endpoint_name or url.split('?')[0]!r} failed after retries: "
                f"{exc.__class__.__name__}"
            ) from None

        if response.status_code == 401 and self._oauth_manager is not None:
            response.close()
            log_event(self._logger, "oauth_token_refresh_failed", run_id=self._run_id, reason="http_401", endpoint=endpoint_name)
            self._oauth_manager.force_refresh()
            # The replay itself still goes through the same bounded retry
            # policy for transient failures (a connection blip during the
            # replay is still worth retrying) -- but if the replay's
            # final response is 401 again, it is returned as-is with no
            # further refresh/replay.
            try:
                response = executor.run(
                    lambda: self._send_once(method, url, params=params),
                    retryable_exceptions=RETRYABLE_EXCEPTIONS,
                    is_retryable_response=_is_retryable_response,
                    get_retry_after=_get_retry_after,
                    close_response=_close,
                )
            except RetryBudgetExhausted as exc:
                raise DownloadError(
                    f"request to endpoint {endpoint_name or url.split('?')[0]!r} exhausted retries after "
                    f"OAuth replay (last status {exc.status_code})"
                ) from None
            except RETRYABLE_EXCEPTIONS as exc:
                raise DownloadError(
                    f"request to endpoint {endpoint_name or url.split('?')[0]!r} failed after retries "
                    f"during OAuth replay: {exc.__class__.__name__}"
                ) from None

        return response

    def fetch_manifest_json(self, manifest_url: str, *, params: dict | None = None) -> dict:
        """`GET manifest_url` (with `params` -- e.g. `endpoint`/`since` --
        passed through httpx's own query-string encoding, never string
        concatenation) and parse the JSON body, streamed and size-bounded
        so an oversized response is aborted before it fills memory."""
        response = self._request_with_retries("GET", manifest_url, params=params, endpoint_name="manifest")
        try:
            if response.status_code == 401:
                raise DownloadError("manifest request was not authorized (HTTP 401)")
            if response.status_code == 403:
                raise DownloadError("manifest request was not authorized (HTTP 403)")
            if response.status_code == 404:
                raise DownloadError("manifest URL returned 404 (not found)")
            if response.status_code != 200:
                raise DownloadError(f"manifest request failed (HTTP {response.status_code})")

            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                body.extend(chunk)
                if len(body) > MAX_MANIFEST_BYTES:
                    raise ManifestError(f"manifest exceeds the {MAX_MANIFEST_BYTES}-byte safety limit")
            try:
                return json.loads(bytes(body))
            except json.JSONDecodeError as exc:
                raise ManifestError(f"manifest is not valid JSON: {exc}") from None
        finally:
            response.close()

    def download_artifact(self, artifact: Artifact, dest_dir: Path) -> DownloadResult:
        """Stream `artifact` into `dest_dir/{table}.csv.part`, verifying
        SHA-256 and byte count as data arrives, then atomically rename to
        `dest_dir/{table}.csv`. Raises DownloadError/ChecksumError and
        removes the partial file on any failure."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = dest_dir / artifact.filename
        part_path = dest_dir / f"{artifact.filename}.part"

        started = time.monotonic()
        hasher = hashlib.sha256()
        bytes_written = 0

        response = self._request_with_retries("GET", artifact.url, endpoint_name=f"artifact:{artifact.table}")
        try:
            if response.status_code == 401:
                raise DownloadError(f"download of {artifact.table!r} was not authorized (HTTP 401)")
            if response.status_code == 403:
                raise DownloadError(f"download of {artifact.table!r} was not authorized (HTTP 403)")
            if response.status_code == 404:
                raise DownloadError(f"artifact for {artifact.table!r} returned 404 (not found)")
            if response.status_code != 200:
                raise DownloadError(f"download of {artifact.table!r} failed (HTTP {response.status_code})")

            size_limit = min(self._max_artifact_bytes, max(artifact.size_bytes * 2, 1024))
            try:
                with open(part_path, "wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        bytes_written += len(chunk)
                        if bytes_written > size_limit:
                            raise DownloadError(
                                f"download of {artifact.table!r} exceeded the {size_limit}-byte size limit"
                            )
                        hasher.update(chunk)
                        fh.write(chunk)
            except BaseException:
                part_path.unlink(missing_ok=True)
                raise
        finally:
            response.close()

        actual_sha256 = hasher.hexdigest()
        if bytes_written != artifact.size_bytes:
            part_path.unlink(missing_ok=True)
            raise DownloadError(
                f"{artifact.table!r}: downloaded {bytes_written} byte(s), manifest declared {artifact.size_bytes}"
            )
        if actual_sha256 != artifact.sha256:
            part_path.unlink(missing_ok=True)
            raise ChecksumError(
                f"{artifact.table!r}: sha256 mismatch (downloaded content does not match the manifest)"
            )

        part_path.replace(final_path)  # atomic rename within the same directory/filesystem
        duration_ms = (time.monotonic() - started) * 1000.0
        return DownloadResult(
            table=artifact.table,
            path=final_path,
            sha256=actual_sha256,
            size_bytes=bytes_written,
            duration_ms=duration_ms,
        )

    def close(self) -> None:
        self._client.close()

    def get_json_page(self, url: str, *, params: dict | None = None, max_bytes: int) -> dict:
        """`GET url` (with `params` -- endpoint/since/page_token/page_size
        -- passed through httpx's own query-string encoding, never string
        concatenation) for the paginated page-request contract (see
        `pagination.py`). Distinct from `fetch_manifest_json` above
        (kept byte-for-byte unchanged for the legacy manifest contract)
        because this path additionally enforces a supported response
        content type before parsing -- one of the paginated envelope's
        own validation rules (see `pagination.validate_page_envelope`'s
        module docstring) that the legacy manifest contract never
        required. Streamed and size-bounded so an oversized page response
        is aborted before it fills memory, exactly like
        `fetch_manifest_json`. Raises `PaginationError` (imported lazily
        to avoid a module-level import cycle) for any non-2xx status
        (after the shared retry machinery in `_request_with_retries` has
        already exhausted retryable ones), unsupported content type,
        oversized body, or invalid JSON.
        """
        from .errors import PaginationError

        response = self._request_with_retries("GET", url, params=params, endpoint_name="page")
        try:
            if response.status_code == 401:
                raise PaginationError("page request was not authorized (HTTP 401)")
            if response.status_code == 403:
                raise PaginationError("page request was not authorized (HTTP 403)")
            if response.status_code == 404:
                raise PaginationError("page request URL returned 404 (not found)")
            if response.status_code != 200:
                raise PaginationError(f"page request failed (HTTP {response.status_code})")

            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";")[0].strip().lower()
            if media_type != "application/json":
                raise PaginationError(
                    f"page response has unsupported content type {content_type!r} (expected application/json)"
                )

            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise PaginationError(f"page response exceeds the {max_bytes}-byte safety limit")
            try:
                return json.loads(bytes(body))
            except json.JSONDecodeError as exc:
                raise PaginationError(f"page response is not valid JSON: {exc}") from None
        finally:
            response.close()
