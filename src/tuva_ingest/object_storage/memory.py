"""A deterministic, in-process fake `StorageBackend` for unit tests that
must never touch a filesystem or network. Same immutability contract as
every other backend (see `base.StorageBackend`): writing different bytes
to an existing key raises `ObjectAlreadyExistsError`; writing identical
bytes is a safe no-op.
"""
from __future__ import annotations

from .base import ObjectAlreadyExistsError, ObjectMeta, ObjectNotFoundError, sha256_bytes


class InMemoryBackend:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> ObjectMeta:
        del content_type
        existing = self._objects.get(key)
        if existing is not None and existing != data:
            raise ObjectAlreadyExistsError(key)
        self._objects[key] = data
        return ObjectMeta(key=key, size_bytes=len(data), sha256=sha256_bytes(data))

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError:
            raise ObjectNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        return key in self._objects

    def head(self, key: str) -> ObjectMeta | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return ObjectMeta(key=key, size_bytes=len(data), sha256=sha256_bytes(data))

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))
