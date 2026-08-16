"""The versioned JSON snapshot manifest contract.

See docs/API_MANIFEST.md for the full human-readable contract. This module
is the single source of truth for validating a fetched manifest: version,
source, snapshot_id, timestamp, and exactly-one-artifact-per-managed-table
with a safe HTTPS URL, nonnegative size, and a lowercase 64-hex sha256.

RAW_TABLES here is the authoritative set of source feeds this connector
extracts and loads into the raw warehouse schema -- the three claims
feeds the Tuva Input Layer's claims sub-part requires (eligibility,
medical_claim, pharmacy_claim; see models/sources.yml and
models/final/*.sql). This connector never extracts, loads, or manages any
other table -- clinical/provider-attribution feeds are out of scope for
this claims-first implementation (see README.md "Architecture").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from .errors import ManifestError

SUPPORTED_MANIFEST_VERSIONS = (1,)

# Keep in exact sync with models/sources.yml and dbt_project.yml's Input
# Layer mapping (models/final/{table}.sql) -- these are the three source
# feeds the Tuva claims Input Layer requires.
RAW_TABLES = (
    "eligibility",
    "medical_claim",
    "pharmacy_claim",
)

_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Artifact:
    table: str
    url: str
    sha256: str
    size_bytes: int

    @property
    def filename(self) -> str:
        return f"{self.table}.csv"


@dataclass(frozen=True)
class Manifest:
    version: int
    source: str
    snapshot_id: str
    created_at: datetime
    artifacts: tuple[Artifact, ...]

    def artifact_for(self, table: str) -> Artifact:
        for artifact in self.artifacts:
            if artifact.table == table:
                return artifact
        raise KeyError(table)


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def parse_and_validate(
    raw: dict, *, allow_insecure_http: bool, expected_tables: tuple[str, ...] | None = None
) -> Manifest:
    """Validate a decoded manifest JSON object and return a `Manifest`.

    Raises `ManifestError` (listing every problem found, not just the
    first) if the manifest is malformed, incomplete, or unsafe.

    `expected_tables` scopes which raw table(s) the `artifacts` list must
    contain -- exactly those, no more, no fewer. Defaults to
    `RAW_TABLES` (every managed table), which is what the legacy,
    full-pipeline `run`/`load-raw` flow still fetches in one manifest.
    The new endpoint-scoped `extract --endpoint <name>` flow (see
    `extract.extract_endpoint_snapshot`) passes a single-table tuple --
    `(endpoints.table_for_endpoint(endpoint),)` -- so a manifest response
    for one endpoint is never rejected for "missing" the other two
    endpoints' artifacts.
    """
    errors: list[str] = []
    required_tables = expected_tables if expected_tables is not None else RAW_TABLES

    if not isinstance(raw, dict):
        raise ManifestError("manifest is not a JSON object")

    version = raw.get("version")
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        errors.append(
            f"manifest version {version!r} is not supported "
            f"(supported: {SUPPORTED_MANIFEST_VERSIONS})"
        )

    source = raw.get("source")
    _require(isinstance(source, str) and source.strip() != "", "manifest 'source' must be a nonempty string", errors)

    snapshot_id = raw.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.match(snapshot_id):
        errors.append(
            f"manifest 'snapshot_id' {snapshot_id!r} is not a safe identifier "
            "(expected up to 128 chars of letters/digits/._- , starting with a letter or digit)"
        )

    created_at_raw = raw.get("created_at")
    created_at = None
    if not isinstance(created_at_raw, str):
        errors.append("manifest 'created_at' must be a string timestamp")
    else:
        try:
            normalized = created_at_raw.replace("Z", "+00:00")
            created_at = datetime.fromisoformat(normalized)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except ValueError:
            errors.append(f"manifest 'created_at' {created_at_raw!r} is not a valid ISO-8601 timestamp")

    artifacts_raw = raw.get("artifacts")
    artifacts: list[Artifact] = []
    seen_tables: dict[str, int] = {}
    if not isinstance(artifacts_raw, list):
        errors.append("manifest 'artifacts' must be a list")
        artifacts_raw = []

    for i, entry in enumerate(artifacts_raw):
        if not isinstance(entry, dict):
            errors.append(f"artifacts[{i}] is not an object")
            continue

        table = entry.get("table")
        url = entry.get("url")
        sha256 = entry.get("sha256")
        size_bytes = entry.get("size_bytes")

        if not isinstance(table, str) or not _TABLE_NAME_RE.match(table):
            errors.append(f"artifacts[{i}].table {table!r} is not a safe lowercase table name")
            continue

        seen_tables[table] = seen_tables.get(table, 0) + 1

        if table not in RAW_TABLES:
            errors.append(f"artifacts[{i}].table {table!r} is not a managed raw table")
        elif table not in required_tables:
            errors.append(
                f"artifacts[{i}].table {table!r} is a managed raw table but was not requested for this "
                f"extraction (expected only: {sorted(required_tables)})"
            )

        if not isinstance(url, str) or not url:
            errors.append(f"artifacts[{i}] ({table}): 'url' must be a nonempty string")
            url = ""
        else:
            parsed = urlparse(url)
            if parsed.scheme == "http" and not allow_insecure_http:
                errors.append(
                    f"artifacts[{i}] ({table}): url uses plain HTTP but insecure HTTP is not allowed"
                )
            elif parsed.scheme not in ("https", "http"):
                errors.append(f"artifacts[{i}] ({table}): url scheme {parsed.scheme!r} is not http(s)")
            if not parsed.netloc:
                errors.append(f"artifacts[{i}] ({table}): url has no host")
            path_segments = parsed.path.split("/")
            if ".." in path_segments:
                errors.append(f"artifacts[{i}] ({table}): url path contains a '..' traversal segment")

        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            errors.append(f"artifacts[{i}] ({table}): sha256 must be a lowercase 64-hex-character string")
            sha256 = sha256 if isinstance(sha256, str) else ""

        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            errors.append(f"artifacts[{i}] ({table}): size_bytes must be a nonnegative integer")
            size_bytes = 0

        artifacts.append(Artifact(table=table, url=url, sha256=sha256, size_bytes=size_bytes))

    duplicates = sorted(t for t, n in seen_tables.items() if n > 1)
    if duplicates:
        errors.append(f"duplicate artifact table name(s): {duplicates}")

    missing = sorted(set(required_tables) - set(seen_tables))
    if missing:
        errors.append(f"missing artifact(s) for managed raw table(s): {missing}")

    unknown = sorted(set(seen_tables) - set(RAW_TABLES))
    if unknown:
        errors.append(f"unknown/unmanaged table(s) present in manifest: {unknown}")

    if errors:
        raise ManifestError(
            f"manifest failed validation ({len(errors)} problem(s)):\n  - " + "\n  - ".join(errors)
        )

    assert created_at is not None  # errors would have been raised above otherwise
    return Manifest(
        version=version,
        source=source,
        snapshot_id=snapshot_id,
        created_at=created_at,
        artifacts=tuple(sorted(artifacts, key=lambda a: a.table)),
    )
