"""The production `StorageBackend`: any S3-compatible object storage
service -- real AWS S3, or a custom `endpoint_url` (e.g. a local MinIO
instance started via `compose.yml`'s `minio` service).

Authentication is always ambient: `boto3.session.Session()`'s own default
credential chain (an IAM role, an assumed role, `AWS_PROFILE`, container/
instance metadata, or a local developer profile) -- exactly the same
pattern `secrets.AwsSecretsManagerProvider` already uses. This module
never accepts, stores, reads, or logs a static AWS access-key/secret-key
pair; see `config.py`'s object-storage settings, which deliberately do
not include one.

`boto3` is imported lazily (inside `S3Backend.__init__`), the same
lazy-import convention `db.py` uses for `psycopg` and `secrets.py` uses
for `boto3` itself, so this module -- and everything that imports it --
stays importable in an environment where `boto3` is not installed.

Immutability / conditional-write behavior
------------------------------------------
`put` first HEADs the key: if it already exists with the same sha256
(recorded as an `x-amz-meta-sha256` object-metadata header on every write
this module performs), the write is a safe no-op. If it exists with a
*different* sha256, this raises `ObjectAlreadyExistsError` without
writing anything.

For a genuinely new key, `put` attempts a conditional `PutObject` with
`IfNoneMatch="*"` (supported by AWS S3 and recent MinIO releases) so two
concurrent publishers racing to write the same key can never have the
second writer silently clobber the first. If the backend rejects the
`IfNoneMatch` parameter itself (an older S3-compatible service that does
not support conditional writes), this module falls back to a plain
`PutObject` -- which narrows, but cannot fully eliminate, a race window
between the initial HEAD and that fallback PUT. This limitation is
inherent to any S3-compatible service without conditional-write support,
not something this connector can work around client-side; see
docs/RUNBOOK.md "Known limitations".
"""
from __future__ import annotations

from ..errors import ObjectStorageError
from .base import ObjectAlreadyExistsError, ObjectMeta, ObjectNotFoundError, sha256_bytes

_SHA256_METADATA_KEY = "sha256"


class S3Backend:
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
            import botocore.exceptions  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ObjectStorageError(
                "the object storage 's3' provider requires the 'boto3' package, which is not "
                "installed (run `uv sync --locked --extra aws`)"
            ) from exc

        self.bucket = bucket
        self._key_prefix = f"{prefix.strip('/')}/" if prefix else ""
        self._botocore_exceptions = botocore.exceptions
        # Ambient credentials only -- see module docstring. `endpoint_url`
        # is the one setting that makes this usable against MinIO/any
        # other S3-compatible service instead of real AWS S3.
        session = boto3.session.Session(region_name=region)
        self._client = session.client("s3", endpoint_url=endpoint_url)

    def _full_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> ObjectMeta:
        existing = self.head(key)
        digest = sha256_bytes(data)
        if existing is not None:
            if existing.sha256 == digest:
                return existing
            raise ObjectAlreadyExistsError(key)

        full_key = self._full_key(key)
        put_kwargs = dict(
            Bucket=self.bucket, Key=full_key, Body=data, ContentType=content_type,
            Metadata={_SHA256_METADATA_KEY: digest},
        )
        try:
            self._client.put_object(IfNoneMatch="*", **put_kwargs)
        except self._botocore_exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("PreconditionFailed", "412"):
                current = self.head(key)
                if current is not None and current.sha256 == digest:
                    return current
                raise ObjectAlreadyExistsError(key) from exc
            if error_code in ("NotImplemented", "InvalidArgument", "501"):
                # Backend does not support conditional PutObject -- see
                # module docstring's documented race-window limitation.
                self._client.put_object(**put_kwargs)
            else:
                raise ObjectStorageError(f"S3 put_object failed for {key!r}: {error_code}") from exc
        return ObjectMeta(key=key, size_bytes=len(data), sha256=digest)

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._full_key(key))
        except self._botocore_exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFoundError(key) from exc
            raise ObjectStorageError(f"S3 get_object failed for {key!r}") from exc
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def head(self, key: str) -> ObjectMeta | None:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=self._full_key(key))
        except self._botocore_exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise ObjectStorageError(f"S3 head_object failed for {key!r}") from exc

        size_bytes = int(response.get("ContentLength", 0))
        metadata = response.get("Metadata", {}) or {}
        digest = metadata.get(_SHA256_METADATA_KEY)
        if digest is None:
            # An object not written by this module (no sha256 metadata) --
            # fall back to a full read to compute the checksum ourselves.
            digest = sha256_bytes(self.get(key))
        return ObjectMeta(key=key, size_bytes=size_bytes, sha256=digest)

    def list(self, prefix: str) -> list[str]:
        full_prefix = self._full_key(prefix)
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for entry in page.get("Contents", []):
                key = entry["Key"]
                if self._key_prefix and key.startswith(self._key_prefix):
                    key = key[len(self._key_prefix):]
                keys.append(key)
        return sorted(keys)
