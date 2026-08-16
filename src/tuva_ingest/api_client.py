"""A production-oriented, streaming, authenticated HTTP client for the
manifest contract (see docs/API_MANIFEST.md).

Design goals, all load-bearing for the "authenticated API client"
capability:
  * Bearer-token auth on every request.
  * Separate connect/read timeouts, bounded retries with exponential
    backoff + jitter, honoring `Retry-After` on 429/5xx.
  * Only 429 and 5xx are retried; 4xx (other than 429) fail immediately.
  * Downloads stream to a temporary `.part` file (never buffered fully in
    memory), verifying SHA-256 and byte count incrementally so an
    oversized or corrupt response is aborted before it fills memory or
    disk unnecessarily; the `.part` file is renamed into place only after
    verification succeeds (atomic completion) and is removed on any
    failure.
  * The token is only ever placed in the Authorization header of the
    underlying request -- it is never interpolated into a log line or an
    exception message.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__
from .errors import ChecksumError, DownloadError, ManifestError
from .manifest import Artifact

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB hard ceiling, independent of declared size
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_MANIFEST_BYTES = 16 * 1024 * 1024  # manifests are small JSON documents


def user_agent() -> str:
    return f"tuva-ingest/{__version__}"


def _backoff_seconds(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return max(0.0, retry_after)
    base = min(2**attempt, 30)
    return base + random.uniform(0, base * 0.25)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form is not implemented; fall back to exponential backoff


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
        timeout_seconds: float,
        max_retries: int,
        logger,
        run_id: str | None = None,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        sleep_fn=time.sleep,
    ) -> None:
        self._token = token
        self._timeout = (timeout_seconds, timeout_seconds)  # (connect, read)
        self._max_retries = max_retries
        self._logger = logger
        self._run_id = run_id
        self._max_artifact_bytes = max_artifact_bytes
        self._sleep_fn = sleep_fn
        self._session = requests.Session()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": user_agent(),
            "Accept": "application/json, text/csv;q=0.9, */*;q=0.1",
        }

    def _request_with_retries(self, method: str, url: str, *, stream: bool) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.request(
                    method, url, headers=self._headers(), timeout=self._timeout, stream=stream
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise DownloadError(f"request to {url!r} failed after {attempt + 1} attempt(s): connection error") from None
                self._sleep_fn(_backoff_seconds(attempt, None))
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                response.close()
                self._sleep_fn(_backoff_seconds(attempt, retry_after))
                continue

            return response

        raise DownloadError(f"request to {url!r} exhausted retries") from last_exc

    def fetch_manifest_json(self, manifest_url: str) -> dict:
        response = self._request_with_retries("GET", manifest_url, stream=True)
        try:
            if response.status_code == 401 or response.status_code == 403:
                raise DownloadError(f"manifest request was not authorized (HTTP {response.status_code})")
            if response.status_code == 404:
                raise DownloadError("manifest URL returned 404 (not found)")
            if response.status_code != 200:
                raise DownloadError(f"manifest request failed (HTTP {response.status_code})")

            body = bytearray()
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
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

        response = self._request_with_retries("GET", artifact.url, stream=True)
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
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
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
        self._session.close()
