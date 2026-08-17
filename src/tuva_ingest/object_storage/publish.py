"""Immutable run publication against a `StorageBackend`: pages first, the
run manifest next, the success marker last -- never relying on
filesystem rename semantics (see `base.StorageBackend`'s immutability
contract). A run is durable and loadable only once its success marker is
written; anything short of that (any subset of pages, or a manifest with
no success marker) must never be treated as a complete, loadable run --
see `verify.py`.

Manifest contents (see docs/SOURCE_CONTRACT.md "Object storage"):
object keys, page numbers, compressed byte sizes, SHA-256 checksums,
record counts, cursor metadata (this run's requested cursor and its
final candidate cursor -- the value that will become the new committed
cursor only after a verified PostgreSQL load/merge, never before, see
`state.commit_cursor`), and extraction timestamps.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..errors import ImmutableObjectError
from .base import ObjectAlreadyExistsError, StorageBackend
from .keys import RunKey

MANIFEST_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def gzip_jsonl(records: list[dict]) -> bytes:
    """Serialize `records` as gzip-compressed JSONL, one exact source
    record per line -- no renaming, coercion, flattening, or reordering
    of values (only each record's own top-level key order is normalized
    via `sort_keys=True`, purely for storage determinism; this changes
    nothing about the JSON value itself). `mtime=0` makes the gzip
    container deterministic for identical input, matching
    `pagination.PaginatedRunStore.write_page`'s existing convention."""
    import io

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for record in records:
            line = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
            gz.write(line.encode("utf-8"))
            gz.write(b"\n")
    return buf.getvalue()


@dataclass(frozen=True)
class PublishedPage:
    page_number: int
    object_key: str
    sha256: str
    compressed_size_bytes: int
    record_count: int
    request_cursor: str | None
    response_cursor: str | None
    next_page_cursor: str | None
    retrieved_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "object_key": self.object_key,
            "sha256": self.sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "record_count": self.record_count,
            "request_cursor": self.request_cursor,
            "response_cursor": self.response_cursor,
            "next_page_cursor": self.next_page_cursor,
            "retrieved_at": self.retrieved_at,
        }


class RunPublisher:
    """One instance per extraction run. Callers publish every page (in
    any order -- pages are independent immutable objects, not a
    sequence that must land in order), then call `publish_manifest`
    exactly once, then `publish_success` exactly once. Calling
    `publish_success` before `publish_manifest`, or referencing a page
    never published through this same instance, is a caller bug (not
    defended against here -- `verify.py`'s independent re-verification
    at load time is what actually protects a downstream reader, exactly
    as it must for values arriving from outside this process, e.g. a
    resumed/retried run)."""

    def __init__(self, backend: StorageBackend, run_key: RunKey) -> None:
        self.backend = backend
        self.run_key = run_key

    def publish_page(
        self,
        page_number: int,
        records: list[dict],
        *,
        request_cursor: str | None,
        response_cursor: str | None,
        next_page_cursor: str | None,
        retrieved_at: str | None = None,
    ) -> PublishedPage:
        body = gzip_jsonl(records)
        key = self.run_key.page_key(page_number)
        try:
            meta = self.backend.put(key, body, content_type="application/gzip")
        except ObjectAlreadyExistsError as exc:
            raise ImmutableObjectError(str(exc)) from exc
        return PublishedPage(
            page_number=page_number,
            object_key=key,
            sha256=meta.sha256,
            compressed_size_bytes=meta.size_bytes,
            record_count=len(records),
            request_cursor=request_cursor,
            response_cursor=response_cursor,
            next_page_cursor=next_page_cursor,
            retrieved_at=retrieved_at or _utc_now_iso(),
        )

    def publish_manifest(
        self,
        *,
        vendor: str,
        endpoint: str,
        requested_cursor: str | None,
        candidate_cursor: str | None,
        pages: list[PublishedPage],
        extraction_started_at: str,
        extraction_finished_at: str | None = None,
    ) -> dict[str, Any]:
        manifest = {
            "version": MANIFEST_VERSION,
            "vendor": self.run_key.vendor,
            "endpoint": endpoint,
            "load_date": self.run_key.load_date.isoformat(),
            "run_id": self.run_key.run_id,
            "prefix": self.run_key.prefix,
            "requested_cursor": requested_cursor,
            "candidate_cursor": candidate_cursor,
            "extraction_started_at": extraction_started_at,
            "extraction_finished_at": extraction_finished_at or _utc_now_iso(),
            "page_count": len(pages),
            "total_record_count": sum(p.record_count for p in pages),
            "pages": [p.to_dict() for p in sorted(pages, key=lambda p: p.page_number)],
            "published_at": _utc_now_iso(),
        }
        body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            self.backend.put(self.run_key.manifest_key, body, content_type="application/json")
        except ObjectAlreadyExistsError as exc:
            raise ImmutableObjectError(str(exc)) from exc
        return manifest

    def publish_success(self, manifest: dict[str, Any]) -> None:
        """Publish the success marker -- the LAST write of a run, only
        after every page and the manifest are already durable (see
        module docstring). Its content is the manifest's own sha256, so
        a corrupted/foreign `_SUCCESS` object is itself caught by the
        same immutability check as every other object, and
        `verify.py` can cheaply confirm the marker actually corresponds
        to the manifest it references."""
        import hashlib

        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        body = (
            json.dumps({"manifest_sha256": manifest_sha256, "published_at": _utc_now_iso()}, sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            self.backend.put(self.run_key.success_key, body, content_type="application/json")
        except ObjectAlreadyExistsError as exc:
            raise ImmutableObjectError(str(exc)) from exc
