"""Operational migration runner for the connector's own control-plane
objects: the raw landing schema, the operational/control schema, and
least-privilege roles/grants (see migrations/001-003).

This module -- and the SQL files under migrations/ it applies -- never
creates, owns, or reproduces any Tuva-managed core, terminology, or
output schema/table. Tuva's own DDL is owned entirely by the pinned
`tuva-health/the_tuva_project` dbt package (see packages.yml); mapping
raw data into it is dbt's job (see models/), not this module's.

Each migration file under migrations/ is a plain, checksum-tracked SQL
file, applied at most once (one_time semantics -- deliberately simpler
than a general-purpose migration runner with repeatable/checksum-
drift-tolerant execution modes, since these three migrations never need
that). Filenames are the ordering key
(`NNN_description.sql`, e.g. `001_operational_schemas.sql`); the numeric
prefix is the tracked `version`.

Dynamic identifiers (`raw_schema`, `ops_schema`, `ingest_role`,
`transform_role`) cannot be represented in static SQL -- PostgreSQL has
no bind-parameter mechanism for schema/role names, and these are all
operator-configurable (see config.py). Each migration file therefore
uses psql-style `:"name"` variable placeholders (identical convention to
this repository's earlier architecture), resolved via
`db.substitute_psql_vars` -- which validates every `:"name"` substitution
against the shared identifier policy (identifiers.py) before it is ever
composed into SQL text, and never accepts a value it hasn't validated
itself, regardless of what the caller already checked (defense in
depth). A migration's checksum is computed over its *raw* file content,
before variable substitution, so it is identical across every
environment/configuration and never depends on RAW_SCHEMA/OPS_SCHEMA/
role names.
"""
from __future__ import annotations

import getpass
import hashlib
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import qualified_relation, quote_ident, substitute_psql_vars, try_advisory_lock, advisory_unlock, MIGRATION_LOCK_KEY
from .errors import LockError, MigrationError
from .identifiers import InvalidIdentifierError, validate_identifier

_VERSION_RE = re.compile(r"^[0-9]{3,}$")
_FILENAME_RE = re.compile(r"^([0-9]{3,})_[a-z0-9_]+\.sql$")


def _validate_identifier(name: str, value: str) -> str:
    try:
        return validate_identifier(value, name)
    except InvalidIdentifierError as exc:
        raise MigrationError(str(exc)) from exc


@dataclass(frozen=True)
class MigrationFile:
    version: str
    filename: str
    path: Path
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    filename: str
    checksum: str
    applied_at: datetime
    duration_ms: float


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[AppliedMigration, ...]
    pending: tuple[MigrationFile, ...]
    checksum_mismatches: tuple[str, ...]

    @property
    def has_integrity_failures(self) -> bool:
        return bool(self.checksum_mismatches)


def _default_migrations_dir(migrations_dir: Path | None) -> Path:
    if migrations_dir is not None:
        return migrations_dir
    return Path(__file__).resolve().parents[2] / "migrations"


def compute_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(migrations_dir: Path | None = None) -> list[MigrationFile]:
    migrations_dir = _default_migrations_dir(migrations_dir)
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    files: list[MigrationFile] = []
    seen_versions: dict[str, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"{path.name}: migration filenames must match 'NNN_description.sql' "
                "(a numeric version prefix, an underscore, a lowercase description, .sql)"
            )
        version = match.group(1)
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version {version!r}: {seen_versions[version]} and {path}")
        seen_versions[version] = path
        files.append(MigrationFile(version=version, filename=path.name, path=path, checksum=compute_checksum(path)))

    if not files:
        raise MigrationError(f"no migration files found under {migrations_dir}")

    files.sort(key=lambda f: f.version)
    return files


def _resolve_vars(config) -> dict[str, str]:
    return {
        "raw_schema": _validate_identifier("RAW_SCHEMA", config.raw_schema),
        "ops_schema": _validate_identifier("OPS_SCHEMA", config.ops_schema),
        "ingest_role": _validate_identifier("INGEST_ROLE", config.ingest_role),
        "transform_role": _validate_identifier("TRANSFORM_ROLE", config.transform_role),
    }


def ensure_history_table(conn, ops_schema: str) -> None:
    """Bootstrap `<ops_schema>.schema_migrations`. Deliberately minimal
    and separate from migrations/001_operational_schemas.sql: the history
    table must exist before any migration (including 001) can be
    tracked, and this function is the sole place that creates it."""
    ops_schema = _validate_identifier("OPS_SCHEMA", ops_schema)
    schema_ident = quote_ident(ops_schema)
    relation = qualified_relation(ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")

    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_ident}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {relation} (
              version      text PRIMARY KEY,
              filename     text NOT NULL,
              checksum     text NOT NULL,
              applied_at   timestamptz NOT NULL,
              duration_ms  double precision NOT NULL
            )
            """
        )
    conn.commit()


def _read_applied(conn, ops_schema: str) -> dict[str, AppliedMigration]:
    ops_schema = _validate_identifier("OPS_SCHEMA", ops_schema)
    relation = qualified_relation(ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")
    applied: dict[str, AppliedMigration] = {}
    with conn.cursor() as cur:
        cur.execute(f"SELECT version, filename, checksum, applied_at, duration_ms FROM {relation} ORDER BY version")
        for version, filename, checksum, applied_at, duration_ms in cur.fetchall():
            applied[version] = AppliedMigration(
                version=version, filename=filename, checksum=checksum, applied_at=applied_at, duration_ms=duration_ms
            )
    return applied


def _history_table_exists(conn, ops_schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'schema_migrations'",
            (ops_schema,),
        )
        return cur.fetchone() is not None


def _plan(all_migrations: list[MigrationFile], applied: dict[str, AppliedMigration]) -> MigrationStatus:
    applied_out: list[AppliedMigration] = []
    pending: list[MigrationFile] = []
    mismatches: list[str] = []

    for migration in all_migrations:
        record = applied.get(migration.version)
        if record is None:
            pending.append(migration)
            continue
        if record.checksum != migration.checksum:
            mismatches.append(migration.version)
            continue
        applied_out.append(record)

    return MigrationStatus(applied=tuple(applied_out), pending=tuple(pending), checksum_mismatches=tuple(mismatches))


def status(conn, config, *, migrations_dir: Path | None = None) -> MigrationStatus:
    """Read-only: never writes, never takes the advisory lock. Validates
    `config.ops_schema` before touching the database (defense in depth,
    matching apply_pending)."""
    _validate_identifier("OPS_SCHEMA", config.ops_schema)
    all_migrations = discover(migrations_dir)

    if not _history_table_exists(conn, config.ops_schema):
        return _plan(all_migrations, {})

    applied = _read_applied(conn, config.ops_schema)
    return _plan(all_migrations, applied)


def apply_pending(conn, config, *, migrations_dir: Path | None = None, logger=None) -> list[AppliedMigration]:
    """Apply every pending migration, in ascending version order, inside
    the migration advisory lock. Refuses to proceed (before executing any
    SQL) if any already-applied migration's checksum has drifted --
    migrations are immutable once applied; add a new migration instead of
    editing one."""
    all_migrations = discover(migrations_dir)

    if not try_advisory_lock(conn, MIGRATION_LOCK_KEY):
        raise LockError(
            "could not acquire the PostgreSQL migration advisory lock -- another migration run "
            "appears to be in progress"
        )
    try:
        ensure_history_table(conn, config.ops_schema)
        applied = _read_applied(conn, config.ops_schema)
        plan = _plan(all_migrations, applied)

        if plan.checksum_mismatches:
            raise MigrationError(
                f"migration(s) {', '.join(plan.checksum_mismatches)} have changed on disk since they "
                "were applied -- checksum mismatch. Migrations are immutable once applied; add a new "
                "migration instead of editing this one."
            )

        variables = _resolve_vars(config)
        relation = qualified_relation(config.ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")
        results: list[AppliedMigration] = []

        for migration in plan.pending:
            sql_text = substitute_psql_vars(migration.path.read_text(encoding="utf-8"), variables)
            started = time.monotonic()
            applied_at = datetime.now(timezone.utc)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql_text)
                    duration_ms = (time.monotonic() - started) * 1000.0
                    cur.execute(
                        f"INSERT INTO {relation} (version, filename, checksum, applied_at, duration_ms) "
                        f"VALUES (%s, %s, %s, %s, %s)",
                        (migration.version, migration.filename, migration.checksum, applied_at, duration_ms),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            result = AppliedMigration(
                version=migration.version, filename=migration.filename, checksum=migration.checksum,
                applied_at=applied_at, duration_ms=duration_ms,
            )
            results.append(result)
            if logger is not None:
                logger(migration.version, migration.filename, duration_ms)

        return results
    finally:
        advisory_unlock(conn, MIGRATION_LOCK_KEY)


def run_host_identity() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "unknown"


def print_status(conn, config) -> int:
    result = status(conn, config)
    if result.has_integrity_failures:
        print(f"CHECKSUM MISMATCH: {', '.join(result.checksum_mismatches)}")
        print("A previously applied migration's file changed on disk. Migrations are immutable once applied.")
        return 1

    print(f"Applied migrations ({len(result.applied)}):")
    for m in result.applied:
        print(f"  {m.version}  {m.filename}  applied_at={m.applied_at.isoformat()}  ({m.duration_ms:.1f} ms)")

    print(f"Pending migrations ({len(result.pending)}):")
    for m in result.pending:
        print(f"  {m.version}  {m.filename}")

    return 0
