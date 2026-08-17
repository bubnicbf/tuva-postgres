"""Object storage: the durable, immutable, replayable source of truth for
extracted source pages (see docs/SOURCE_CONTRACT.md "Object storage" and
README.md "Architecture").

This package is deliberately small and interface-first (`base.StorageBackend`)
so the rest of the connector -- key construction (`keys.py`), publication
(`publish.py`), verification (`verify.py`), and the loader
(`object_raw_loader.py`) -- never couples directly to a specific backend
(S3, MinIO, local filesystem, or the in-memory fake used by unit tests).

Backends:
  `local.LocalFilesystemBackend`  a real (but non-production) filesystem
                                  backend for local development, kept
                                  separate from the legacy
                                  `pagination.PaginatedRunStore`'s
                                  directory-rename-based store -- this
                                  backend never relies on filesystem
                                  rename semantics for atomicity (see its
                                  module docstring); it exists only so the
                                  object-storage code path is exercisable
                                  without a real S3-compatible service.
  `memory.InMemoryBackend`        a deterministic in-process fake, for unit
                                  tests that must never touch a filesystem
                                  or network.
  `s3.S3Backend`                  the production backend: any S3-compatible
                                  service (AWS S3, or a custom
                                  `endpoint_url` such as MinIO), authenticated
                                  via boto3's ambient credential chain only.
"""
from __future__ import annotations

from .base import ObjectAlreadyExistsError, ObjectMeta, ObjectNotFoundError, StorageBackend

__all__ = ["StorageBackend", "ObjectMeta", "ObjectAlreadyExistsError", "ObjectNotFoundError"]
