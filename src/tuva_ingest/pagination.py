"""Paginated JSON extraction: request one page at a time, validate the
response envelope, and land each page as an immutable, checksummed,
gzip-compressed JSONL file -- the mechanism behind `tuva-ingest
extract`/`load`/`sync` (see `endpoints.py` for the supported
`--endpoint` values, `docs/SOURCE_CONTRACT.md` "Pagination" for the full
operational contract this module implements, and `docs/API_MANIFEST.md`
for the legacy full-manifest CSV contract this module does *not* touch --
that one still backs `load-raw`/`run`, unchanged).

Response envelope (minimal, documented contract -- this repository does
not integrate a specific named vendor, so these are this connector's own
field names, chosen and fixed here since none were established
elsewhere):

    {
      "records": [ {...}, {...} ],
      "metadata": {
        "record_count": 2,
        "page_token": "<echo of the requested page token, or null on page 1>",
        "next_page_token": "<token for the next page, or null/absent when this is the final page>",
        "high_water_mark": "<opaque, lexicographically-sortable candidate watermark>"
      }
    }

`endpoint`, `since` (the prior watermark or an operator-supplied
`--since` override), `page_token`, and `page_size` are always sent as
real httpx query parameters (see `api_client.ApiClient.get_json_page`),
never concatenated into the URL.

Run identifier: `extract_paginated_run` mints a fresh `run_id` for every
invocation (`{endpoint}-{utc timestamp}-{random suffix}`), the same
minting shape `cli._cmd_run`/`_cmd_load_raw` already use for their own
run ids. This is a deliberate choice, not an oversight: unlike the
legacy CSV/manifest contract (whose `snapshot_id` is a stable value the
*source* itself assigns, making natural request-parameter-based
idempotency possible), a paginated incremental extraction's identity is
inherently the specific attempt, not a reproducible function of
`(endpoint, since)` alone -- two `sync --endpoint eligibility` calls an
hour apart, with no explicit `--since`, are expected to each pull
whatever is new since the last committed watermark and therefore *must*
produce two different, independently loadable runs, not collapse into
"identical, return the existing one". `PaginatedRunStore.check_existing_run`
still implements the "repeating an identical extraction returns the
existing run only after verifying checksums; a conflicting run_id fails
loudly" requirement as a defense-in-depth guarantee about the *storage
layer* itself (what happens if a `run_id` collision ever occurs, by any
means) -- see its docstring below.
"""
from __future__ import annotations

import gzip
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api_client import ApiClient
from .endpoints import table_for_endpoint
from .errors import PaginationError
from .logging_utils import log_event

DIR_MODE = 0o750
FILE_MODE = 0o640
SUCCESS_MARKER = "_SUCCESS"
MANIFEST_FILENAME = "manifest.json"
STAGING_DIRNAME = ".staging"
PAGES_DIRNAME = "pages"  # kept separate from the legacy snapshot layout under RAW_DATA_DIR/{source}/{snapshot_id}/


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def file_sha256(path: Path) -> str:
    """Public (unlike most of this module's other file-local helpers)
    because `paginated_loader.verify_run_manifest` re-verifies page
    checksums independently at load time and reuses this exact
    implementation rather than keeping a second copy."""
    import hashlib

    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


_file_sha256 = file_sha256  # internal alias, matches this module's own call sites below


# --- envelope validation -----------------------------------------------


@dataclass(frozen=True)
class PageEnvelope:
    records: tuple[dict, ...]
    record_count: int
    page_token: str | None
    next_page_token: str | None
    high_water_mark: str


def validate_page_envelope(payload: Any, *, requested_page_token: str | None) -> PageEnvelope:
    """Validate one page response against the minimal paginated envelope
    contract (see module docstring) before anything is written to disk.
    Raises `PaginationError` (listing exactly what is wrong) for any
    violation -- a non-object envelope, a missing/non-array `records`, a
    non-object record, missing/invalid `metadata`, a `record_count` that
    is not a non-negative integer or does not match the actual number of
    records, a returned page token that does not match the requested
    one, an invalid `next_page_token`, or a missing/invalid
    `high_water_mark`. Never raises for `next_page_token` being null or
    absent -- that is the documented final-page signal, not an error."""
    if not isinstance(payload, dict):
        raise PaginationError(f"page response envelope must be a JSON object, got {type(payload).__name__}")

    if "records" not in payload:
        raise PaginationError("page response envelope is missing required field 'records'")
    records = payload["records"]
    if not isinstance(records, list):
        raise PaginationError(f"'records' must be a JSON array, got {type(records).__name__}")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise PaginationError(f"records[{index}] must be a JSON object, got {type(record).__name__}")

    if "metadata" not in payload:
        raise PaginationError("page response envelope is missing required field 'metadata'")
    metadata = payload["metadata"]
    if not isinstance(metadata, dict):
        raise PaginationError(f"'metadata' must be a JSON object, got {type(metadata).__name__}")

    if "record_count" not in metadata:
        raise PaginationError("metadata.record_count is required")
    record_count = metadata["record_count"]
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise PaginationError(f"metadata.record_count must be a non-negative integer, got {record_count!r}")
    if record_count != len(records):
        raise PaginationError(
            f"metadata.record_count ({record_count}) does not match the actual number of records "
            f"in this response ({len(records)})"
        )

    page_token = metadata.get("page_token")
    if page_token is not None and not isinstance(page_token, str):
        raise PaginationError(f"metadata.page_token must be a string or null, got {type(page_token).__name__}")
    if requested_page_token is not None and page_token is not None and page_token != requested_page_token:
        raise PaginationError(
            f"metadata.page_token {page_token!r} does not match the requested page token "
            f"{requested_page_token!r}"
        )

    next_page_token = metadata.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise PaginationError(
            f"metadata.next_page_token must be a string or null, got {type(next_page_token).__name__}"
        )
    if next_page_token == "":
        raise PaginationError("metadata.next_page_token must not be an empty string (use null/absent instead)")

    if "high_water_mark" not in metadata:
        raise PaginationError("metadata.high_water_mark is required")
    high_water_mark = metadata["high_water_mark"]
    if not isinstance(high_water_mark, str) or not high_water_mark:
        raise PaginationError("metadata.high_water_mark must be a non-empty string")

    return PageEnvelope(
        records=tuple(records),
        record_count=record_count,
        page_token=page_token,
        next_page_token=next_page_token,
        high_water_mark=high_water_mark,
    )


# --- immutable page files + run manifest --------------------------------


@dataclass(frozen=True)
class PageMetadata:
    run_id: str
    endpoint: str
    page_number: int
    request_page_token: str | None
    response_page_token: str | None
    next_page_token: str | None
    record_count: int
    retrieved_at: str
    file_name: str
    compressed_size_bytes: int
    sha256: str
    candidate_high_water_mark: str

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            "page_number": self.page_number,
            "request_page_token": self.request_page_token,
            "response_page_token": self.response_page_token,
            "next_page_token": self.next_page_token,
            "record_count": self.record_count,
            "retrieved_at": self.retrieved_at,
            "file_name": self.file_name,
            "compressed_size_bytes": self.compressed_size_bytes,
            "sha256": self.sha256,
            "candidate_high_water_mark": self.candidate_high_water_mark,
        }


class PaginatedRunStore:
    """The immutable, atomic, on-disk layout for one paginated extraction
    run -- the pagination-contract analogue of `extract.RawSnapshotStore`,
    kept as a separate class (rather than extended/shared) because the
    two contracts' manifest shapes, file formats (gzip JSONL vs. CSV),
    and publication granularity (page-by-page vs. all-artifacts-at-once)
    are different enough that sharing one class would blur both.

    Layout::

        RAW_DATA_DIR/
          {source}/
            pages/
              {run_id}/
                page-000001.jsonl.gz
                page-000002.jsonl.gz
                ...
                manifest.json
                _SUCCESS
              .staging/{run_id}-{token}/   (temporary, removed on failure)

    Guarantees mirror `RawSnapshotStore`: every page lands in staging
    first, `_SUCCESS` is written only after every page is written and the
    manifest is complete, publication is a single atomic directory
    rename, a completed run is never overwritten, and a load operation
    must reject any run missing its `_SUCCESS` marker or manifest.
    """

    def __init__(self, raw_data_dir: Path, source: str) -> None:
        self.raw_data_dir = Path(raw_data_dir)
        self.source = source
        self.source_dir = self.raw_data_dir / source / PAGES_DIRNAME

    def run_dir(self, run_id: str) -> Path:
        return self.source_dir / run_id

    def _staging_root(self) -> Path:
        return self.source_dir / STAGING_DIRNAME

    def is_published(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / SUCCESS_MARKER).is_file()

    def read_manifest(self, run_id: str) -> dict:
        return json.loads((self.run_dir(run_id) / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    def begin_staging(self, run_id: str) -> Path:
        self._staging_root().mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        token = secrets.token_hex(8)
        staging_dir = self._staging_root() / f"{run_id}-{token}"
        staging_dir.mkdir(mode=DIR_MODE)
        return staging_dir

    def abort_staging(self, staging_dir: Path) -> None:
        if staging_dir.exists() and staging_dir.parent == self._staging_root():
            for child in sorted(staging_dir.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                else:
                    child.rmdir()
            staging_dir.rmdir()

    def write_page(
        self,
        staging_dir: Path,
        *,
        run_id: str,
        endpoint: str,
        page_number: int,
        request_page_token: str | None,
        envelope: PageEnvelope,
        retrieved_at: datetime,
    ) -> PageMetadata:
        """Write `envelope.records` as gzip-compressed JSONL -- one exact
        source record per line, values/structure preserved exactly as
        received (no renaming, coercion, flattening, null-stripping, or
        reordering) -- to a temporary `.part` file, flush and close it,
        checksum the exact stored compressed bytes, then atomically
        rename to its immutable final name. `mtime=0` makes the gzip
        output deterministic (the only source of non-determinism gzip
        itself introduces) for identical input on the same Python/zlib
        version -- "deterministic where practical", not an absolute
        cross-platform guarantee."""
        file_name = f"page-{page_number:06d}.jsonl.gz"
        final_path = staging_dir / file_name
        part_path = staging_dir / f"{file_name}.part"

        with open(part_path, "wb") as raw_fh:
            with gzip.GzipFile(fileobj=raw_fh, mode="wb", mtime=0) as gz:
                for record in envelope.records:
                    line = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
                    gz.write(line.encode("utf-8"))
                    gz.write(b"\n")
            raw_fh.flush()
            os.fsync(raw_fh.fileno())

        sha256 = _file_sha256(part_path)
        size_bytes = part_path.stat().st_size
        part_path.chmod(FILE_MODE)
        part_path.replace(final_path)  # atomic rename within the same directory/filesystem

        return PageMetadata(
            run_id=run_id,
            endpoint=endpoint,
            page_number=page_number,
            request_page_token=request_page_token,
            response_page_token=envelope.page_token,
            next_page_token=envelope.next_page_token,
            record_count=envelope.record_count,
            retrieved_at=_iso(retrieved_at),
            file_name=file_name,
            compressed_size_bytes=size_bytes,
            sha256=sha256,
            candidate_high_water_mark=envelope.high_water_mark,
        )

    def finalize(
        self,
        staging_dir: Path,
        run_id: str,
        pages: list[PageMetadata],
        *,
        endpoint: str,
        since: str | None,
        total_record_count: int,
        candidate_high_water_mark: str,
    ) -> Path:
        final_dir = self.run_dir(run_id)
        if final_dir.exists():
            raise PaginationError(
                f"run directory {final_dir} already exists -- refusing to overwrite a completed, "
                "immutable run"
            )

        manifest = {
            "version": 1,
            "run_id": run_id,
            "source": self.source,
            "endpoint": endpoint,
            "since": since,
            "pages": [p.to_dict() for p in pages],
            "page_count": len(pages),
            "total_record_count": total_record_count,
            "candidate_high_water_mark": candidate_high_water_mark,
            "published_at": _iso(_utc_now()),
        }
        (staging_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        for entry in staging_dir.iterdir():
            if entry.is_file():
                entry.chmod(FILE_MODE)
        staging_dir.chmod(DIR_MODE)

        success_path = staging_dir / SUCCESS_MARKER
        success_path.write_text("", encoding="utf-8")
        success_path.chmod(FILE_MODE)

        self.source_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.rename(staging_dir, final_dir)
        return final_dir

    def check_existing_run(self, run_id: str) -> dict | None:
        """If `run_id` is already published, verify its manifest and every
        page file's checksum, then return the manifest so the caller can
        treat this as an idempotent no-op. Returns `None` if `run_id` has
        never been published. Raises `PaginationError` if `run_id` is
        published but corrupted (a missing page file or checksum
        mismatch) -- a conflicting/corrupted run must fail loudly, never
        be silently reused or overwritten."""
        if not self.is_published(run_id):
            return None
        manifest = self.read_manifest(run_id)
        run_dir = self.run_dir(run_id)
        for page in manifest["pages"]:
            page_path = run_dir / page["file_name"]
            if not page_path.is_file():
                raise PaginationError(
                    f"published run {run_id!r} is missing page file {page['file_name']!r} -- "
                    "refusing to treat a corrupted run as reusable"
                )
            actual_sha256 = _file_sha256(page_path)
            if actual_sha256 != page["sha256"]:
                raise PaginationError(
                    f"published run {run_id!r} page {page['file_name']!r} has sha256 {actual_sha256} "
                    f"but the manifest recorded {page['sha256']!r} -- refusing to treat a corrupted "
                    "run as reusable"
                )
        return manifest


# --- extraction orchestration --------------------------------------------


@dataclass(frozen=True)
class PaginatedExtractResult:
    run_id: str
    endpoint: str
    table: str
    since: str | None
    path: Path
    skipped: bool
    page_count: int
    total_record_count: int
    candidate_high_water_mark: str


def _mint_run_id(endpoint: str) -> str:
    return f"{endpoint}-{_utc_now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}"


def extract_paginated_run(
    config: Any,
    client: ApiClient,
    logger: Any,
    *,
    endpoint: str,
    since: str | None = None,
) -> PaginatedExtractResult:
    """Extract one endpoint's paginated data, one page at a time, into a
    fresh, immutable run. Requests exactly one page per HTTP call (no
    prefetching); stops only when a validated response's `next_page_token`
    is null/absent. Never advances any durable high-water mark -- that
    only happens transactionally at load/commit time (see
    `paginated_loader.py`). Raises `PaginationError` (envelope violation,
    repeated/cyclic token, max-page-count exceeded) or `DownloadError`
    (HTTP/connection failure after retries are exhausted) on any failure,
    always cleaning up its staging directory first (see the `finally`
    block below) -- a failed extraction never leaves a partially-staged
    run at the published path.
    """
    table = table_for_endpoint(endpoint)
    store = PaginatedRunStore(config.raw_data_dir, config.source_name)

    # run_id is minted fresh below and is vanishingly unlikely to already
    # exist, but if it somehow does (e.g. a mocked/injected id in a test,
    # or a pathological clock/PRNG scenario), check_existing_run's
    # verify-then-reuse-or-fail-loudly contract still applies rather than
    # silently overwriting whatever is already published there.
    run_id = _mint_run_id(endpoint)
    existing = store.check_existing_run(run_id)
    if existing is not None:
        log_event(logger, "extract_skipped_already_published", run_id=run_id, endpoint=endpoint)
        return PaginatedExtractResult(
            run_id=run_id, endpoint=endpoint, table=table, since=since, path=store.run_dir(run_id),
            skipped=True, page_count=existing["page_count"], total_record_count=existing["total_record_count"],
            candidate_high_water_mark=existing["candidate_high_water_mark"],
        )

    staging_dir = store.begin_staging(run_id)

    pages: list[PageMetadata] = []
    seen_request_tokens: set[str] = set()
    seen_next_tokens: set[str] = set()
    total_record_count = 0
    request_token: str | None = None
    page_number = 0
    candidate_high_water_mark: str | None = None

    try:
        while True:
            page_number += 1
            if page_number > config.api_max_pages:
                raise PaginationError(
                    f"pagination for endpoint {endpoint!r} exceeded the configured maximum of "
                    f"{config.api_max_pages} pages (TUVA_API_MAX_PAGES) -- aborting as a defense "
                    "against an infinite pagination loop"
                )

            if request_token is not None:
                if request_token in seen_request_tokens:
                    raise PaginationError(
                        f"pagination cycle detected: page token {request_token!r} was requested more "
                        "than once in this run"
                    )
                seen_request_tokens.add(request_token)

            params: dict[str, str] = {"endpoint": endpoint}
            if since is not None:
                params["since"] = since
            if request_token is not None:
                params["page_token"] = request_token
            if config.api_page_size is not None:
                params["page_size"] = str(config.api_page_size)

            log_event(logger, "page_request_started", run_id=run_id, endpoint=endpoint, page_number=page_number)
            started = time.monotonic()
            payload = client.get_json_page(
                config.api_manifest_url, params=params, max_bytes=config.api_max_page_bytes
            )
            duration_ms = (time.monotonic() - started) * 1000.0
            log_event(
                logger, "page_request_completed", run_id=run_id, endpoint=endpoint, page_number=page_number,
                duration_ms=duration_ms,
            )

            envelope = validate_page_envelope(payload, requested_page_token=request_token)
            log_event(
                logger, "page_validated", run_id=run_id, endpoint=endpoint, page_number=page_number,
                record_count=envelope.record_count,
            )

            if envelope.next_page_token is not None:
                if envelope.next_page_token in seen_next_tokens or envelope.next_page_token == request_token:
                    raise PaginationError(
                        f"pagination cycle detected: next_page_token {envelope.next_page_token!r} repeats "
                        "a previously seen token in this run"
                    )
                seen_next_tokens.add(envelope.next_page_token)

            retrieved_at = _utc_now()
            page_meta = store.write_page(
                staging_dir, run_id=run_id, endpoint=endpoint, page_number=page_number,
                request_page_token=request_token, envelope=envelope, retrieved_at=retrieved_at,
            )
            pages.append(page_meta)
            total_record_count += envelope.record_count
            candidate_high_water_mark = envelope.high_water_mark
            log_event(
                logger, "page_file_published", run_id=run_id, endpoint=endpoint, page_number=page_number,
                sha256=page_meta.sha256, record_count=envelope.record_count,
            )

            if envelope.next_page_token is None:
                break
            request_token = envelope.next_page_token

        assert candidate_high_water_mark is not None  # at least one page is always written before this point
        final_dir = store.finalize(
            staging_dir, run_id, pages, endpoint=endpoint, since=since,
            total_record_count=total_record_count, candidate_high_water_mark=candidate_high_water_mark,
        )
        log_event(
            logger, "pagination_completed", run_id=run_id, endpoint=endpoint, page_number=page_number,
            page_count=len(pages), record_count=total_record_count,
        )
    except Exception:
        store.abort_staging(staging_dir)
        raise

    return PaginatedExtractResult(
        run_id=run_id, endpoint=endpoint, table=table, since=since, path=final_dir, skipped=False,
        page_count=len(pages), total_record_count=total_record_count,
        candidate_high_water_mark=candidate_high_water_mark,
    )
