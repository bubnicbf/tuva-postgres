"""Select and construct the configured `StorageBackend` from
`IngestConfig` (see `config.py`'s `OBJECT_STORAGE_PROVIDER`/
`OBJECT_STORAGE_BUCKET`/`OBJECT_STORAGE_PREFIX`/`OBJECT_STORAGE_REGION`/
`OBJECT_STORAGE_ENDPOINT_URL`). Kept as one small function so nothing
else in this connector needs to know how many providers exist or how
each is constructed.
"""
from __future__ import annotations

from typing import Any

from ..errors import ConfigError
from .base import StorageBackend

SUPPORTED_PROVIDERS: tuple[str, ...] = ("local", "s3")


def build_backend(config: Any) -> StorageBackend:
    provider = config.object_storage_provider
    if provider == "local":
        from .local import LocalFilesystemBackend

        if not config.object_storage_local_root:
            raise ConfigError(
                "OBJECT_STORAGE_PROVIDER=local requires OBJECT_STORAGE_LOCAL_ROOT to be set"
            )
        return LocalFilesystemBackend(config.object_storage_local_root)

    if provider == "s3":
        from .s3 import S3Backend

        if not config.object_storage_bucket:
            raise ConfigError("OBJECT_STORAGE_PROVIDER=s3 requires OBJECT_STORAGE_BUCKET to be set")
        return S3Backend(
            bucket=config.object_storage_bucket,
            prefix=None,  # the vendor/endpoint/... key prefix is applied by keys.py, not the bucket prefix
            region=config.object_storage_region,
            endpoint_url=config.object_storage_endpoint_url,
        )

    raise ConfigError(  # pragma: no cover - IngestConfig already rejects this before construction
        f"unsupported OBJECT_STORAGE_PROVIDER {provider!r} (supported: {', '.join(SUPPORTED_PROVIDERS)})"
    )
