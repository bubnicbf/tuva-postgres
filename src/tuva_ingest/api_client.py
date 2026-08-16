"""A production-oriented, streaming, authenticated HTTP client for the
manifest contract (see docs/API_MANIFEST.md), built on `httpx` (a
reusable, explicitly-timed-out `httpx.Client`) with bounded retries
driven directly by `tenacity`.

Design goals, all load-bearing for the "authenticated API client"
capability:
  * Bearer-token auth on every request; the token is only ever placed in
    the Authorization header of the underlying request -- it is never
    interpolated into a log line or an exception message.
  * Explicit connect/read/write/pool timeouts (`IngestConfig.httpx_timeout()`),
    bounded retries with exponential backoff + jitter, honoring
    `Retry-After` on 429/5xx (bounded by `max_retry_delay_seconds` --
    never an unbounded sleep).
  * Only httpx connection/timeout errors, HTTP 429, and HTTP
    500/502/503/504 are retried -- via `tenacity.Retrying` with
    `stop_after_attempt(max_retries + 1)`, never an unbounded retry loop.
    Ordinary 4xx (400/401/403/404), validation failures, and checksum
    failures are never retried.
  * Redirects are never followed automatically (`follow_redirects=False`)
    -- a manifest/artifact URL redirecting to an unexpected host must
    never silently receive this client's bearer token.
  * Downloads stream to a temporary `.part` file (never buffered fully in
    memory), verifying SHA-256 and byte count incrementally so an
    oversized or corrupt response is aborted before it fills memory or
    disk unnecessarily; the `.part` file is renamed into place only after
    verification succeeds (atomic completion) and is removed on any
    failure.
  * A real `httpx.BaseTransport` (e.g. `httpx.MockTransport`) can be
    injected via `transport=` -- this is how `tests/unit/test_api_client.py`
    exercises every retry/auth/checksum path with zero real network
    access, per this repository's testing policy.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

from . import __version__
from .errors import ChecksumError, DownloadError, ManifestError
from .logging_utils import log_event
from .manifest import Artifact

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB hard ceiling, independent of declared size
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_MANIFEST_BYTES = 16 * 1024 * 1024  # manifests are small JSON documents

# httpx exception umbrella classes that represent transient, retry-worthy
# failures. `httpx.TimeoutException` covers Connect/Read/Write/PoolTimeout;
# `httpx.NetworkError` covers Connect/Read/Write/CloseError (DNS failures,
# connection resets, refused connections, ...). Neither includes
# `httpx.HTTPStatusError` (this client never calls `raise_for_status()`)
# or any 4xx/programming error -- those are never retried.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.NetworkError)


def user_agent() -> str:
    return f"tuva-ingest/{__version__}"


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form is not implemented; fall back to exponential backoff


class _RetryableHttpStatus(Exception):
    """Raised internally to route a retryable (429/5xx) HTTP response
    through the same `tenacity` retry machinery as a connection/timeout
    exception. Never escapes `_request_with_retries` -- always translated
    into a `DownloadError` there."""

    def __init__(self, status_code: int, retry_after: str | None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"retryable HTTP status {status_code}")


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
        token: str,
        timeout_seconds: float = 30.0,
        timeout: "httpx.Timeout | None" = None,
        max_retries: int = 5,
        max_retry_delay_seconds: float = 30.0,
        logger: Any = None,
        run_id: str | None = None,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        sleep_fn: Any = time.sleep,
        transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        self._token = token
        self._max_retries = max_retries
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._logger = logger
        self._run_id = run_id
        self._max_artifact_bytes = max_artifact_bytes
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

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": user_agent(),
            "Accept": "application/json, text/csv;q=0.9, */*;q=0.1",
        }

    def _wait(self, retry_state: RetryCallState) -> float:
        """Bounded exponential backoff with jitter; honors a valid
        `Retry-After` value from a 429/5xx response instead of the
        exponential schedule, but never sleeps longer than
        `max_retry_delay_seconds` regardless of which source produced the
        delay -- an unbounded sleep (from a hostile or misconfigured
        Retry-After) is never allowed."""
        attempt = max(retry_state.attempt_number - 1, 0)
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, _RetryableHttpStatus):
            retry_after = _parse_retry_after(exc.retry_after)
            if retry_after is not None:
                return max(0.0, min(retry_after, self._max_retry_delay_seconds))
        base = min(2**attempt, self._max_retry_delay_seconds)
        return min(base + random.uniform(0, base * 0.25), self._max_retry_delay_seconds)

    def _retrying(self) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type((*RETRYABLE_EXCEPTIONS, _RetryableHttpStatus)),
            sleep=self._sleep_fn,
            reraise=True,
        )

    def _log_retry(self, event: str, **fields: Any) -> None:
        if self._logger is not None:
            log_event(self._logger, event, run_id=self._run_id, **fields)

    def _request_with_retries(self, method: str, url: str, *, params: dict | None = None) -> httpx.Response:
        """Issue one logical request, transparently retrying transient
        failures (connection/timeout errors, 429, 500/502/503/504) up to
        `max_retries` additional times. Returns the streamed
        `httpx.Response` (status already known, body not yet read) for
        the caller to inspect/consume -- callers own `response.close()`.
        """
        response: httpx.Response | None = None
        try:
            for attempt in self._retrying():
                with attempt:
                    request = self._client.build_request(method, url, headers=self._headers(), params=params)
                    response = self._client.send(request, stream=True)
                    if response.status_code in RETRYABLE_STATUS:
                        retry_after = response.headers.get("Retry-After")
                        status_code = response.status_code
                        response.close()
                        self._log_retry(
                            "http_retry_scheduled",
                            url=url,
                            status_code=status_code,
                            attempt=attempt.retry_state.attempt_number,
                        )
                        raise _RetryableHttpStatus(status_code, retry_after)
        except _RetryableHttpStatus as exc:
            raise DownloadError(
                f"request to {url!r} exhausted retries (last status {exc.status_code})"
            ) from None
        except RETRYABLE_EXCEPTIONS as exc:
            raise DownloadError(
                f"request to {url!r} failed after retries: {exc.__class__.__name__}"
            ) from None

        assert response is not None  # the loop above always either returns or raises
        return response

    def fetch_manifest_json(self, manifest_url: str, *, params: dict | None = None) -> dict:
        """`GET manifest_url` (with `params` -- e.g. `endpoint`/`since` --
        passed through httpx's own query-string encoding, never string
        concatenation) and parse the JSON body, streamed and size-bounded
        so an oversized response is aborted before it fills memory."""
        response = self._request_with_retries("GET", manifest_url, params=params)
        try:
            if response.status_code in (401, 403):
                raise DownloadError(f"manifest request was not authorized (HTTP {response.status_code})")
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

        response = self._request_with_retries("GET", artifact.url)
        try:
            if response.status_code in (401, 403):
                raise DownloadError(f"download of {artifact.table!r} was not authorized (HTTP {response.status_code})")
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
