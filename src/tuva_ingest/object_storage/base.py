"""The storage-backend interface every object-storage backend implements
(`local.LocalFilesystemBackend`, `memory.InMemoryBackend`, `s3.S3Backend`).

Deliberately small: publication (`publish.py`) and verification
(`verify.py`) are built entirely on these five operations, so a new
backend never needs to reimplement immutable-publication or
verify-before-load semantics -- it only needs to implement safe object
storage/retrieval.

Immutability contract (enforced by every backend, not just S3): `put`
must never silently overwrite an existing object with *different*
content. Writing the exact same bytes to an existing key is a safe,
idempotent no-op (this is what makes retrying a partially-published run
safe); writing different bytes to an existing key raises
`ObjectAlreadyExistsError`. No backend here relies on filesystem rename
semantics for this guarantee -- `local.LocalFilesystemBackend` uses a
content check, and `s3.S3Backend` uses a conditional/if-none-match-style
write plus a follow-up integrity check, exactly mirroring what a real
S3-compatible service supports (see its module docstring for the exact
mechanism and its documented limitations).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ObjectMeta:
    key: str
    size_bytes: int
    sha256: str


class ObjectAlreadyExistsError(Exception):
    """Raised by `put` when `key` already holds different content than
    what is being written -- a completed, immutable object/manifest/success
    marker must never be silently overwritten (see module docstring)."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"object {key!r} already exists with different content -- refusing to overwrite an "
            "immutable published object"
        )


class ObjectNotFoundError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"object {key!r} does not exist")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StorageBackend(Protocol):
    """The minimal interface `publish.py`/`verify.py`/`object_raw_loader.py`
    depend on. Every method operates on a full object key (already built
    by `keys.py`) -- no backend here knows anything about the
    vendor/endpoint/load_date/run_id/page-number key structure itself."""

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> ObjectMeta:
        """Write `data` to `key`. Idempotent for identical content (a safe
        no-op returning the existing object's metadata); raises
        `ObjectAlreadyExistsError` if `key` already holds *different*
        content. Never partially writes a key that other readers could
        observe mid-write (see each backend's own docstring for exactly
        how it achieves this)."""
        ...

    def get(self, key: str) -> bytes:
        """Return the exact bytes stored at `key`. Raises
        `ObjectNotFoundError` if `key` does not exist."""
        ...

    def exists(self, key: str) -> bool: ...

    def head(self, key: str) -> ObjectMeta | None:
        """Return `key`'s size/sha256 without reading its full body when the
        backend can do so cheaply, or `None` if `key` does not exist."""
        ...

    def list(self, prefix: str) -> list[str]:
        """Every key under `prefix`, in no particular guaranteed order."""
        ...
