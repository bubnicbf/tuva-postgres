"""Immutable raw landing layer.

Layout::

    RAW_DATA_DIR/
      {source}/
        {snapshot_id}/
          manifest.json
          {table}.csv            (one per managed table)
          checksums.json
          _SUCCESS
        current                  (text file containing the latest published snapshot_id)
        .staging/{snapshot_id}-{token}/   (temporary, removed on failure)

Guarantees (see docstrings below for how each is implemented):
  * downloads land in a temporary sibling directory, never directly at the
    final path;
  * `_SUCCESS` is written only after every artifact is validated;
  * publication is a single atomic directory rename;
  * a completed snapshot is never overwritten;
  * re-fetching an identical, already-completed snapshot is a no-op
    (idempotent); re-fetching the same snapshot_id with different content
    is a loud failure;
  * `current` only ever advances after a successful publish;
  * raw snapshots are never deleted automatically.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import LandingError

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


class RawLandingLayer:
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
        manifest to `manifest`. Returns True if the fetch can be safely
        skipped (identical content already landed). Raises LandingError
        if the same snapshot_id already exists with *different* content
        -- that must never be silently accepted."""
        if not self.is_published(snapshot_id):
            return False
        existing = self.read_manifest(snapshot_id)
        if existing == manifest:
            return True
        raise LandingError(
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
            raise LandingError(
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
