"""Independent re-verification of a published run, read back entirely
from object storage -- never trusts the manifest's own numbers blindly,
and never treats a run as loadable without a valid success marker (see
`publish.py`'s pages-then-manifest-then-success-marker ordering).

Every check here is re-derived from the actual stored bytes: existence,
SHA-256 checksum, gzip container integrity, decompressed JSONL record
count, and (summed across every page) reconciliation against the
manifest's own `total_record_count`/`page_count`. This is the same
defense-in-depth posture `paginated_loader.verify_run_manifest` already
applies to the legacy local-filesystem contract, applied here to object
storage instead.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterator

from ..errors import ObjectVerificationError, RunNotPublishedError
from .base import ObjectNotFoundError, StorageBackend, sha256_bytes
from .keys import RunKey


@dataclass(frozen=True)
class VerifiedRun:
    manifest: dict[str, Any]
    run_key: RunKey


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def load_and_verify_manifest(backend: StorageBackend, run_key: RunKey) -> VerifiedRun:
    """Re-verify a run end to end and return it only if every check
    passes. Raises `RunNotPublishedError` if the success marker or
    manifest is missing; raises `ObjectVerificationError` (listing every
    problem found) for any checksum, gzip, record-count, or
    reconciliation mismatch. A run failing either check must never be
    loaded into PostgreSQL."""
    if not backend.exists(run_key.success_key):
        raise RunNotPublishedError(
            f"run {run_key.run_id!r} has no success marker at {run_key.success_key!r} -- refusing to "
            "load a run that was not fully published (see docs/SOURCE_CONTRACT.md 'Publication and "
            "success-marker semantics')"
        )
    if not backend.exists(run_key.manifest_key):
        raise RunNotPublishedError(
            f"run {run_key.run_id!r} has a success marker but no manifest at {run_key.manifest_key!r} -- "
            "refusing to load a corrupted/incomplete run"
        )

    success_body = backend.get(run_key.success_key)
    try:
        success_doc = json.loads(success_body)
    except json.JSONDecodeError as exc:
        raise ObjectVerificationError(f"run {run_key.run_id!r}: success marker is not valid JSON: {exc}") from None

    manifest_body = backend.get(run_key.manifest_key)
    try:
        manifest = json.loads(manifest_body)
    except json.JSONDecodeError as exc:
        raise ObjectVerificationError(f"run {run_key.run_id!r}: manifest is not valid JSON: {exc}") from None

    errors: list[str] = []

    expected_manifest_sha256 = success_doc.get("manifest_sha256")
    actual_manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
    _require(
        expected_manifest_sha256 == actual_manifest_sha256,
        f"success marker references manifest sha256 {expected_manifest_sha256!r} but the manifest at "
        f"{run_key.manifest_key!r} actually hashes to {actual_manifest_sha256!r}",
        errors,
    )

    pages = manifest.get("pages")
    if not isinstance(pages, list):
        errors.append("manifest 'pages' must be a list")
        pages = []

    summed_record_count = 0
    for page in pages:
        object_key = page.get("object_key")
        if not isinstance(object_key, str):
            errors.append(f"page {page.get('page_number')!r}: missing/invalid object_key")
            continue
        try:
            body = backend.get(object_key)
        except ObjectNotFoundError:
            errors.append(f"page {page.get('page_number')!r}: object {object_key!r} is missing from storage")
            continue

        actual_sha256 = sha256_bytes(body)
        expected_sha256 = page.get("sha256")
        if actual_sha256 != expected_sha256:
            errors.append(
                f"page {page.get('page_number')!r} ({object_key!r}): sha256 {actual_sha256} does not match "
                f"the manifest's recorded {expected_sha256!r}"
            )
            continue

        try:
            record_count = _count_gzip_jsonl_records(body)
        except OSError as exc:
            errors.append(f"page {page.get('page_number')!r} ({object_key!r}): not a valid gzip stream: {exc}")
            continue

        expected_record_count = page.get("record_count")
        if record_count != expected_record_count:
            errors.append(
                f"page {page.get('page_number')!r} ({object_key!r}): contains {record_count} record(s) but "
                f"the manifest recorded record_count={expected_record_count}"
            )
            continue

        summed_record_count += record_count

    expected_total = manifest.get("total_record_count")
    _require(
        summed_record_count == expected_total,
        f"sum of page record counts ({summed_record_count}) does not equal the manifest's "
        f"total_record_count ({expected_total})",
        errors,
    )
    _require(
        len(pages) == manifest.get("page_count"),
        f"number of pages in the manifest ({len(pages)}) does not equal its own page_count "
        f"({manifest.get('page_count')})",
        errors,
    )

    if errors:
        raise ObjectVerificationError(
            f"run {run_key.run_id!r} failed verification ({len(errors)} problem(s)):\n  - " + "\n  - ".join(errors)
        )

    return VerifiedRun(manifest=manifest, run_key=run_key)


def _count_gzip_jsonl_records(body: bytes) -> int:
    import io

    count = 0
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        for line in gz:
            if line.strip():
                count += 1
    return count


def iter_verified_page_records(backend: StorageBackend, page: dict[str, Any]) -> Iterator[dict]:
    """Stream one page's records without ever loading a whole run into
    memory at once (see `object_raw_loader.py`, which calls this once
    per page inside its page-by-page COPY loop). Callers that need
    strict per-page verification first should already have called
    `load_and_verify_manifest`, which re-verifies every page's checksum
    and record count up front; this function trusts that verification
    already happened and simply re-parses the same bytes."""
    import io

    body = backend.get(page["object_key"])
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        for line in gz:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)
