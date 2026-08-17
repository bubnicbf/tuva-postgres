"""Object-storage-backed paginated extraction -- the object-storage
analogue of `pagination.extract_paginated_run`, reusing the exact same
page request/response envelope contract (`pagination.validate_page_envelope`)
and `ApiClient`. This EXTENDS the existing endpoint-scoped extract/load/
sync workflow (see cli.py) rather than replacing it or standing up an
unrelated parallel pipeline: same `--endpoint` vocabulary
(`endpoints.py`), same page request loop shape, same cycle/max-page
defenses. Only the publication target differs -- object storage (see
`object_storage/publish.py`) instead of a local directory (see
`pagination.PaginatedRunStore`, which remains available unchanged for
`OBJECT_STORAGE_PROVIDER=local` local development/CI use, and for the
legacy, fully filesystem-only `extract`/`load`/`sync` commands, which
this module does not touch or alter).

Extraction never advances any cursor -- `state.commit_cursor` is only
ever called from `object_raw_loader.py`'s single atomic load
transaction, and only after a full verify+load+reconcile succeeds (see
docs/SOURCE_CONTRACT.md "Cursor safety"). Object publication alone
(reaching a durable `_SUCCESS` marker) is likewise never sufficient to
advance the cursor by itself.

This module DOES write a small amount of operational audit state during
extraction (`ingestion_run.status` 'running' -> 'published', via
`state.create_ingestion_run`/`mark_run_published`) -- both auto-commit
immediately (see state.py's module-section docstring for why), so a run
that crashes mid-extraction is still visible to operators as 'running'
rather than vanishing entirely. Neither of these two calls is inside --
or shares a transaction with -- the later PostgreSQL load transaction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import state
from .api_client import ApiClient
from .endpoint_contract import normalized_endpoint
from .errors import PaginationError
from .logging_utils import log_event
from .object_storage.base import StorageBackend
from .object_storage.keys import build_run_key, new_run_id, utc_load_date
from .object_storage.publish import RunPublisher

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class ObjectExtractResult:
    run_id: str
    endpoint: str
    vendor: str
    since: str | None
    run_prefix: str
    manifest: dict
    page_count: int
    total_record_count: int
    candidate_cursor: str


def extract_to_object_storage(
    conn,
    config,
    client: ApiClient,
    backend: StorageBackend,
    logger,
    *,
    endpoint: str,
    since: str | None = None,
) -> ObjectExtractResult:
    from .pagination import validate_page_envelope  # re-use the exact same envelope contract

    normalized = normalized_endpoint(endpoint)
    vendor = config.source_name
    run_id = new_run_id()
    load_date = utc_load_date()
    run_key = build_run_key(
        prefix=config.object_storage_prefix, vendor=vendor, endpoint=normalized, load_date=load_date, run_id=run_id,
    )
    publisher = RunPublisher(backend, run_key)

    extraction_started_at = _utc_now_iso()
    state.create_ingestion_run(
        conn, config.ops_schema, run_id=run_id, vendor=vendor, endpoint=normalized, load_date=load_date,
        storage_bucket=getattr(config, "object_storage_bucket", None), storage_run_prefix=run_key.run_prefix,
        requested_cursor=since, app_version=_app_version(), environment=config.pipeline_environment,
    )
    log_event(logger, "object_extract_started", run_id=run_id, endpoint=normalized, vendor=vendor, since=since)

    pages = []
    seen_request_tokens: set[str] = set()
    seen_next_tokens: set[str] = set()
    total_record_count = 0
    request_token: str | None = None
    page_number = 0
    candidate_cursor: str | None = None

    while True:
        page_number += 1
        if page_number > config.api_max_pages:
            raise PaginationError(
                f"pagination for endpoint {endpoint!r} exceeded the configured maximum of "
                f"{config.api_max_pages} pages (TUVA_API_MAX_PAGES) -- aborting as a defense against an "
                "infinite pagination loop"
            )

        if request_token is not None:
            if request_token in seen_request_tokens:
                raise PaginationError(
                    f"pagination cycle detected: page token {request_token!r} was requested more than "
                    "once in this run"
                )
            seen_request_tokens.add(request_token)

        params: dict[str, str] = {"endpoint": endpoint}
        if since is not None:
            params["since"] = since
        if request_token is not None:
            params["page_token"] = request_token
        if config.api_page_size is not None:
            params["page_size"] = str(config.api_page_size)

        log_event(logger, "page_request_started", run_id=run_id, endpoint=normalized, page_number=page_number)
        started = time.monotonic()
        payload = client.get_json_page(config.api_manifest_url, params=params, max_bytes=config.api_max_page_bytes)
        duration_ms = (time.monotonic() - started) * 1000.0
        log_event(
            logger, "page_request_completed", run_id=run_id, endpoint=normalized, page_number=page_number,
            duration_ms=duration_ms,
        )

        envelope = validate_page_envelope(payload, requested_page_token=request_token)

        if envelope.next_page_token is not None:
            if envelope.next_page_token in seen_next_tokens or envelope.next_page_token == request_token:
                raise PaginationError(
                    f"pagination cycle detected: next_page_token {envelope.next_page_token!r} repeats a "
                    "previously seen token in this run"
                )
            seen_next_tokens.add(envelope.next_page_token)

        published_page = publisher.publish_page(
            page_number,
            list(envelope.records),
            request_cursor=request_token,
            response_cursor=envelope.page_token,
            next_page_cursor=envelope.next_page_token,
        )
        pages.append(published_page)
        total_record_count += envelope.record_count
        candidate_cursor = envelope.high_water_mark
        log_event(
            logger, "page_published", run_id=run_id, endpoint=normalized, page_number=page_number,
            object_key=published_page.object_key, sha256=published_page.sha256, record_count=envelope.record_count,
        )

        if envelope.next_page_token is None:
            break
        request_token = envelope.next_page_token

    assert candidate_cursor is not None  # at least one page is always published before this point

    manifest = publisher.publish_manifest(
        vendor=vendor, endpoint=normalized, requested_cursor=since, candidate_cursor=candidate_cursor,
        pages=pages, extraction_started_at=extraction_started_at,
    )
    publisher.publish_success(manifest)
    log_event(
        logger, "object_run_published", run_id=run_id, endpoint=normalized, page_count=len(pages),
        record_count=total_record_count, candidate_cursor=candidate_cursor,
    )

    state.mark_run_published(
        conn, config.ops_schema, run_id, candidate_cursor=candidate_cursor, page_count=len(pages),
        extracted_count=total_record_count,
    )

    return ObjectExtractResult(
        run_id=run_id, endpoint=normalized, vendor=vendor, since=since, run_prefix=run_key.run_prefix,
        manifest=manifest, page_count=len(pages), total_record_count=total_record_count,
        candidate_cursor=candidate_cursor,
    )


def _app_version() -> str:
    from . import __version__

    return __version__
