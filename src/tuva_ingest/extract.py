"""Extraction orchestration and the immutable raw landing layer.

This module owns two things:

1. `RawSnapshotStore` -- the atomic, immutable, on-disk snapshot layout
   (ported from this repository's earlier direct-loader architecture,
   where it was proven under `tests/integration`). A snapshot is only
   ever considered "published" once every artifact has been downloaded
   and verified and a `_SUCCESS` marker has been written -- a partial
   download can never appear complete to any other reader.
2. `extract_snapshot()` -- orchestrates a full extraction: fetch +
   validate the manifest, skip early if that exact snapshot is already
   published (idempotent), download every artifact into a staging
   directory, and publish atomically.

Layout::

    RAW_DATA_DIR/
      {source}/
        {snapshot_id}/
          manifest.json
          {table}.csv            (one per raw table: eligibility,
                                   medical_claim, pharmacy_claim)
          checksums.json
          _SUCCESS
        current                  (text file containing the latest published snapshot_id)
        .staging/{snapshot_id}-{token}/   (temporary, removed on failure)

Guarantees:
  * downloads land in a temporary sibling directory, never directly at the
    final path;
  * `_SUCCESS` is written only after every artifact is validated;
  * publication is a single atomic directory rename;
  * a completed snapshot is never overwritten;
  * re-extracting an identical, already-completed snapshot is a no-op
    (idempotent); re-extracting the same snapshot_id with different
    content is a loud failure, never a silent overwrite;
  * `current` only ever advances after a successful publish;
  * raw snapshots are never deleted automatically.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from .api_client import ApiClient
from .errors import ExtractError
from .logging_utils import log_event
from .manifest import Manifest, parse_and_validate

DIR_MODE = 0o750
FILE_MODE = 0o640
SUCCESS_MARKER = "_SUCCESS"
MANIFEST_FILENAME = "manifest.json"
CHECKSUMS_FILENAME = "checksums.json"
CURRENT_POINTER = "current"
STAGING_DIRNAME = ".staging"


@dataclass(frozen=True)
class PublishedSnapshot:
    path: Path
    snapshot_id: str
    manifest: dict
    checksums: dict


class RawSnapshotStore:
    def __init__(self, raw_data_dir: Path, source: str) -> None:
        self.raw_data_dir = Path(raw_data_dir)
        self.source = source
        self.source_dir = self.raw_data_dir / source

    # --- paths ---------------------------------------------------------
    def snapshot_dir(self, snapshot_id: str) -> Path:
        return self.source_dir / snapshot_id

    def _staging_root(self) -> Path:
        return self.source_dir / STAGING_DIRNAME

    def _current_pointer_path(self) -> Path:
        return self.source_dir / CURRENT_POINTER

    # --- read-only queries ----------------------------------------------
    def is_published(self, snapshot_id: str) -> bool:
        return (self.snapshot_dir(snapshot_id) / SUCCESS_MARKER).is_file()

    def read_manifest(self, snapshot_id: str) -> dict:
        return json.loads((self.snapshot_dir(snapshot_id) / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    def read_checksums(self, snapshot_id: str) -> dict:
        return json.loads((self.snapshot_dir(snapshot_id) / CHECKSUMS_FILENAME).read_text(encoding="utf-8"))

    def current_snapshot_id(self) -> str | None:
        pointer = self._current_pointer_path()
        if not pointer.is_file():
            return None
        value = pointer.read_text(encoding="utf-8").strip()
        return value or None

    # --- staging lifecycle -----------------------------------------------
    def begin_staging(self, snapshot_id: str) -> Path:
        """Create and return a fresh temporary sibling directory for
        downloading a snapshot's artifacts into. Never the final path."""
        self._staging_root().mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        token = secrets.token_hex(8)
        staging_dir = self._staging_root() / f"{snapshot_id}-{token}"
        staging_dir.mkdir(mode=DIR_MODE)
        return staging_dir

    def abort_staging(self, staging_dir: Path) -> None:
        """Remove a temporary staging directory after a failed fetch. The
        `current` pointer and any previously completed snapshot are
        untouched."""
        if staging_dir.exists() and staging_dir.parent == self._staging_root():
            for child in sorted(staging_dir.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                else:
                    child.rmdir()
            staging_dir.rmdir()

    def check_idempotent_or_conflicting(self, snapshot_id: str, manifest: dict) -> bool:
        """If `snapshot_id` is already published, compare the stored
        manifest to `manifest`. Returns True if the extraction can be
        safely skipped (identical content already landed). Raises
        ExtractError if the same snapshot_id already exists with
        *different* content -- that must never be silently accepted."""
        if not self.is_published(snapshot_id):
            return False
        existing = self.read_manifest(snapshot_id)
        if existing == manifest:
            return True
        raise ExtractError(
            f"snapshot {snapshot_id!r} for source {self.source!r} is already published with "
            "different content than the manifest just fetched -- refusing to overwrite a "
            "completed, immutable snapshot. Investigate before retrying with a new snapshot_id."
        )

    def finalize(self, staging_dir: Path, snapshot_id: str, manifest: dict, checksums: dict) -> PublishedSnapshot:
        """Write manifest.json/checksums.json/_SUCCESS into the staging
        dir, apply restrictive permissions, then atomically rename it into
        its final immutable location and advance the `current` pointer.
        Must only be called after every artifact has been downloaded and
        verified."""
        final_dir = self.snapshot_dir(snapshot_id)
        if final_dir.exists():
            raise ExtractError(
                f"snapshot directory {final_dir} already exists -- refusing to overwrite a "
                "completed snapshot"
            )

        (staging_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (staging_dir / CHECKSUMS_FILENAME).write_text(json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        for entry in staging_dir.iterdir():
            if entry.is_file():
                entry.chmod(FILE_MODE)
        staging_dir.chmod(DIR_MODE)

        # _SUCCESS is written last, after everything else is in place and
        # permissioned, so its presence is a reliable "this snapshot is
        # complete" signal for any concurrent reader.
        success_path = staging_dir / SUCCESS_MARKER
        success_path.write_text("", encoding="utf-8")
        success_path.chmod(FILE_MODE)

        self.source_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.rename(staging_dir, final_dir)  # atomic: same filesystem (staging is a sibling)

        self._advance_current(snapshot_id)

        return PublishedSnapshot(path=final_dir, snapshot_id=snapshot_id, manifest=manifest, checksums=checksums)

    def _advance_current(self, snapshot_id: str) -> None:
        pointer = self._current_pointer_path()
        tmp_pointer = pointer.with_name(f".{CURRENT_POINTER}.tmp-{secrets.token_hex(4)}")
        tmp_pointer.write_text(snapshot_id, encoding="utf-8")
        tmp_pointer.chmod(FILE_MODE)
        os.replace(tmp_pointer, pointer)  # atomic rename; only ever called after a successful publish


@dataclass(frozen=True)
class ExtractResult:
    snapshot_id: str
    path: Path
    skipped: bool  # True if an identical snapshot was already published
    manifest: Manifest


def extract_snapshot(config, client: ApiClient, logger) -> ExtractResult:
    """Fetch + validate the manifest, then either skip (identical
    snapshot already published) or download every artifact and publish
    atomically. Raises on any failure; never leaves a partially-staged
    snapshot at the final path (see `RawSnapshotStore.finalize`)."""
    manifest_raw = client.fetch_manifest_json(config.api_manifest_url)
    manifest = parse_and_validate(manifest_raw, allow_insecure_http=config.api_allow_insecure_http)
    log_event(logger, "manifest_fetched", snapshot_id=manifest.snapshot_id, artifact_count=len(manifest.artifacts))

    store = RawSnapshotStore(config.raw_data_dir, config.source_name)
    if store.check_idempotent_or_conflicting(manifest.snapshot_id, manifest_raw):
        log_event(logger, "extract_skipped_already_published", snapshot_id=manifest.snapshot_id)
        return ExtractResult(
            snapshot_id=manifest.snapshot_id,
            path=store.snapshot_dir(manifest.snapshot_id),
            skipped=True,
            manifest=manifest,
        )

    staging_dir = store.begin_staging(manifest.snapshot_id)
    checksums: dict[str, dict] = {}
    try:
        for artifact in manifest.artifacts:
            log_event(logger, "artifact_download_started", table=artifact.table)
            result = client.download_artifact(artifact, staging_dir)
            log_event(logger, "artifact_download_completed", table=artifact.table, duration_ms=result.duration_ms)
            checksums[artifact.table] = {"sha256": result.sha256, "size_bytes": result.size_bytes}
    except Exception:
        # Any failure -- a download error, a checksum mismatch, a
        # keyboard interrupt -- removes the staging directory so no
        # partial snapshot is ever left where a reader could mistake it
        # for a real one. The final path is only ever touched by
        # `finalize()`, which never runs on this path.
        store.abort_staging(staging_dir)
        raise

    published = store.finalize(staging_dir, manifest.snapshot_id, manifest_raw, checksums)
    log_event(logger, "raw_snapshot_published", snapshot_id=manifest.snapshot_id, raw_path=str(published.path))
    return ExtractResult(snapshot_id=manifest.snapshot_id, path=published.path, skipped=False, manifest=manifest)
