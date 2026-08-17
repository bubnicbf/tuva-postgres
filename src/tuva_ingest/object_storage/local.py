"""A real (but non-production) filesystem-backed `StorageBackend`, for
local development and deterministic tests that want to exercise actual
file I/O without a running S3-compatible service.

This is deliberately NOT the same mechanism as the legacy
`pagination.PaginatedRunStore` (which publishes a whole run via a single
atomic directory rename). Publication atomicity here is achieved the same
way `object_storage/publish.py` achieves it against a *real* object store
(immutable page objects first, the manifest next, the success marker
last, each individually content-checked before being considered
"already written") -- this backend exists only to let that same
publication code path run against a local disk, never to reintroduce
rename-based atomicity as a crutch. See `base.StorageBackend`'s module
docstring for the exact immutability contract every backend (including
this one) must uphold.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..errors import ObjectStorageError
from .base import ObjectAlreadyExistsError, ObjectMeta, ObjectNotFoundError, sha256_bytes

FILE_MODE = 0o640
DIR_MODE = 0o750


class LocalFilesystemBackend:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in Path(key).parts:
            raise ObjectStorageError(f"object key {key!r} is not a safe relative path")
        return self.root / key

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> ObjectMeta:
        del content_type  # not represented on a plain filesystem
        path = self._path(key)
        if path.is_file():
            existing = path.read_bytes()
            if existing == data:
                return ObjectMeta(key=key, size_bytes=len(existing), sha256=sha256_bytes(existing))
            raise ObjectAlreadyExistsError(key)

        path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        # Write-to-temp-then-rename within the same directory: this is a
        # single-writer local convenience (never assumed by publish.py to
        # be what makes publication atomic -- that guarantee comes from
        # the content-equality check above plus the pages-then-manifest-
        # then-success-marker ordering enforced by publish.py itself, the
        # same ordering `object_storage.s3.S3Backend` operates under,
        # where no rename primitive exists at all).
        tmp_path = path.with_suffix(path.suffix + f".part-{os.getpid()}-{id(data)}")
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.chmod(FILE_MODE)
        os.replace(tmp_path, path)
        return ObjectMeta(key=key, size_bytes=len(data), sha256=sha256_bytes(data))

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def head(self, key: str) -> ObjectMeta | None:
        path = self._path(key)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return ObjectMeta(key=key, size_bytes=len(data), sha256=sha256_bytes(data))

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if base.is_dir():
            search_root = base
        else:
            search_root = base.parent
        if not search_root.is_dir():
            return []
        results: list[str] = []
        for candidate in search_root.rglob("*"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(self.root).as_posix()
            if rel.startswith(prefix):
                results.append(rel)
        return sorted(results)
