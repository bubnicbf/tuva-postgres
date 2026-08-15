"""Migration history and runner.

Migrations are discovered from `db/migrations/*.json` manifests (NOT the
`.sql` files directly -- a manifest lists, in a fixed deterministic order,
which file(s) make up that migration). This lets migration "0001_baseline"
reference the existing, already-reviewed `db/tables/*.sql` schema
(dependency-safe ordered: see db/migrations/0001_baseline.json) without
duplicating ~90 files into a second location -- there is exactly one
source of truth for the DDL. Later migrations (0002+) are ordinary new
`.sql` files under db/migrations/sql/.

Each migration's checksum is a SHA-256 over its ordered constituent
files' contents (framed with path+length so reordering or truncation
changes the hash); a later edit to any file a migration manifest
references is therefore detected as a checksum mismatch on the next run,
exactly like editing an already-applied migration file directly.

`schema_migrations` itself is bootstrapped directly by this module (not
sourced from a migration file) -- it must exist before any migration can
be tracked. Everything else migrations create (pipeline_runs,
pipeline_artifacts, ...) is a normal tracked migration.

No `ON_ERROR_STOP` here (that's a psql-specific flag) -- the equivalent
guarantee is structural: `cursor.execute()` raises on the first SQL
error, which aborts the whole migration's transaction and this runner
does not proceed to the next pending migration.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .db import connect, substitute_psql_vars, try_advisory_lock, advisory_unlock, MIGRATION_LOCK_KEY
from .errors import MigrationError, LockError
from .manifest import MANAGED_TABLES

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION_RE = re.compile(r"^[0-9]{4,}$")


@dataclass(frozen=True)
class MigrationDef:
    version: str
    description: str
    files: tuple[Path, ...]
    var_map: dict[str, str]  # sql placeholder name -> PipelineConfig attribute name
    manifest_path: Path


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    description: str
    checksum: str
    applied_at: datetime
    duration_ms: float
    app_version: str


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[AppliedMigration, ...]
    pending: tuple[MigrationDef, ...]
    checksum_mismatches: tuple[str, ...]


def _validate_identifier(name: str, value: str) -> None:
    if not _IDENTIFIER_RE.match(value):
        raise MigrationError(f"{name}={value!r} is not a safe SQL identifier")


def discover(migrations_dir: Path, repo_root: Path) -> list[MigrationDef]:
    manifest_paths = sorted(migrations_dir.glob("*.json"))
    if not manifest_paths:
        raise MigrationError(f"no migration manifests found under {migrations_dir}")

    migrations: list[MigrationDef] = []
    seen_versions: dict[str, Path] = {}
    for manifest_path in manifest_paths:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MigrationError(f"{manifest_path}: invalid JSON ({exc})") from None

        version = raw.get("version")
        description = raw.get("description")
        files = raw.get("files")
        var_map = raw.get("vars", {})

        if not isinstance(version, str) or not _VERSION_RE.match(version):
            raise MigrationError(f"{manifest_path}: 'version' must be a numeric string like '0001'")
        if not isinstance(description, str) or not description.strip():
            raise MigrationError(f"{manifest_path}: 'description' must be a nonempty string")
        if not isinstance(files, list) or not files:
            raise MigrationError(f"{manifest_path}: 'files' must be a nonempty list")

        if version in seen_versions:
            raise MigrationError(
                f"duplicate migration version {version!r}: {seen_versions[version]} and {manifest_path}"
            )
        seen_versions[version] = manifest_path

        resolved_files = []
        for rel in files:
            path = (repo_root / rel).resolve()
            if not path.is_file():
                raise MigrationError(f"{manifest_path}: referenced file not found: {rel}")
            resolved_files.append(path)

        migrations.append(
            MigrationDef(
                version=version,
                description=description,
                files=tuple(resolved_files),
                var_map=dict(var_map),
                manifest_path=manifest_path,
            )
        )

    migrations.sort(key=lambda m: m.version)
    return migrations


def compute_checksum(migration: MigrationDef) -> str:
    hasher = hashlib.sha256()
    for path in migration.files:
        content = path.read_bytes()
        hasher.update(f"{path.name}:{len(content)}:".encode("utf-8"))
        hasher.update(content)
    return hasher.hexdigest()


def _resolve_vars(migration: MigrationDef, config) -> dict[str, str]:
    resolved = {}
    for placeholder, config_attr in migration.var_map.items():
        value = getattr(config, config_attr.lower(), None)
        if value is None:
            # var_map values are UPPER_SNAKE env-var-style names in the manifest
            value = getattr(config, config_attr.lower())
        _validate_identifier(f"{migration.version}.vars.{placeholder}", str(value))
        resolved[placeholder] = str(value)
    return resolved


def _rendered_sql(migration: MigrationDef, variables: dict[str, str]) -> str:
    parts = [path.read_text(encoding="utf-8") for path in migration.files]
    return substitute_psql_vars("\n".join(parts), variables)


def ensure_history_table(conn, ops_schema: str) -> None:
    _validate_identifier("OPS_SCHEMA", ops_schema)
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{ops_schema}"')
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{ops_schema}".schema_migrations (
              version      text PRIMARY KEY,
              description  text NOT NULL,
              checksum     text NOT NULL,
              applied_at   timestamptz NOT NULL,
              duration_ms  double precision NOT NULL,
              app_version  text NOT NULL
            )
            """
        )
    conn.commit()


def _history_table_exists(conn, ops_schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'schema_migrations'",
            (ops_schema,),
        )
        return cur.fetchone() is not None


def _read_applied(conn, ops_schema: str) -> dict[str, AppliedMigration]:
    applied: dict[str, AppliedMigration] = {}
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT version, description, checksum, applied_at, duration_ms, app_version '
            f'FROM "{ops_schema}".schema_migrations ORDER BY version'
        )
        for version, description, checksum, applied_at, duration_ms, app_version in cur.fetchall():
            applied[version] = AppliedMigration(
                version=version,
                description=description,
                checksum=checksum,
                applied_at=applied_at,
                duration_ms=duration_ms,
                app_version=app_version,
            )
    return applied


def status(conn, config) -> MigrationStatus:
    """Read-only: never writes, never takes a lock. Safe to run anytime,
    including concurrently with an in-progress migration."""
    migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations"
    repo_root = migrations_dir.parents[1]
    all_migrations = discover(migrations_dir, repo_root)

    if not _history_table_exists(conn, config.ops_schema):
        return MigrationStatus(applied=(), pending=tuple(all_migrations), checksum_mismatches=())

    applied = _read_applied(conn, config.ops_schema)
    mismatches = []
    pending = []
    for migration in all_migrations:
        checksum = compute_checksum(migration)
        if migration.version in applied:
            if applied[migration.version].checksum != checksum:
                mismatches.append(migration.version)
        else:
            pending.append(migration)

    return MigrationStatus(
        applied=tuple(applied[v] for v in sorted(applied)),
        pending=tuple(pending),
        checksum_mismatches=tuple(mismatches),
    )


def _target_schema_nonempty(conn, pg_schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'patient'",
            (pg_schema,),
        )
        return cur.fetchone() is not None


def _verify_baseline_compatible(conn, pg_schema: str) -> list[str]:
    """Best-effort compatibility check for `--baseline-existing`: every
    managed table must exist in pg_schema and have a primary key. Not
    exhaustive (it does not compare full column sets) -- it exists to
    catch "this is clearly not the expected schema" rather than to
    replace the DDL's own guarantees."""
    problems = []
    with conn.cursor() as cur:
        for table in MANAGED_TABLES:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                (pg_schema, table),
            )
            if cur.fetchone() is None:
                problems.append(f"expected table {pg_schema}.{table} does not exist")
                continue
            cur.execute(
                """
                SELECT 1 FROM pg_constraint c
                JOIN pg_class r ON r.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = r.relnamespace
                WHERE n.nspname = %s AND r.relname = %s AND c.contype = 'p'
                """,
                (pg_schema, table),
            )
            if cur.fetchone() is None:
                problems.append(f"expected table {pg_schema}.{table} has no primary key")
    return problems


def _apply_one(conn, migration: MigrationDef, config) -> AppliedMigration:
    variables = _resolve_vars(migration, config)
    sql_text = _rendered_sql(migration, variables)
    checksum = compute_checksum(migration)
    started = time.monotonic()
    applied_at = datetime.now(timezone.utc)

    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            duration_ms = (time.monotonic() - started) * 1000.0
            cur.execute(
                f'INSERT INTO "{config.ops_schema}".schema_migrations '
                f"(version, description, checksum, applied_at, duration_ms, app_version) "
                f"VALUES (%s, %s, %s, %s, %s, %s)",
                (migration.version, migration.description, checksum, applied_at, duration_ms, __version__),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return AppliedMigration(
        version=migration.version,
        description=migration.description,
        checksum=checksum,
        applied_at=applied_at,
        duration_ms=duration_ms,
        app_version=__version__,
    )


def apply_pending(conn, config, *, baseline_existing: bool = False, logger=None) -> list[AppliedMigration]:
    migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations"
    repo_root = migrations_dir.parents[1]
    all_migrations = discover(migrations_dir, repo_root)

    if not try_advisory_lock(conn, MIGRATION_LOCK_KEY):
        raise LockError(
            "could not acquire the PostgreSQL migration advisory lock -- another migration run "
            "appears to be in progress"
        )
    try:
        ensure_history_table(conn, config.ops_schema)
        applied = _read_applied(conn, config.ops_schema)

        for migration in all_migrations:
            if migration.version in applied:
                checksum = compute_checksum(migration)
                if applied[migration.version].checksum != checksum:
                    raise MigrationError(
                        f"migration {migration.version} ({migration.manifest_path.name}) has changed since it "
                        "was applied -- checksum mismatch. Migrations are immutable once applied; add a new "
                        "migration instead of editing this one."
                    )

        first_version = all_migrations[0].version if all_migrations else None
        applied_results: list[AppliedMigration] = []
        for migration in all_migrations:
            if migration.version in applied:
                continue

            if migration.version == first_version and _target_schema_nonempty(conn, config.pg_schema):
                if not baseline_existing:
                    raise MigrationError(
                        f"schema {config.pg_schema!r} already contains managed tables but has no migration "
                        f"history for {migration.version} -- refusing to silently stamp an unknown existing "
                        "database as migrated. Re-run with baseline_existing=True (CLI: "
                        "`tuva-postgres migrate --baseline-existing`) after verifying this is the expected schema."
                    )
                problems = _verify_baseline_compatible(conn, config.pg_schema)
                if problems:
                    raise MigrationError(
                        f"--baseline-existing verification failed for schema {config.pg_schema!r}:\n  - "
                        + "\n  - ".join(problems)
                    )

            result = _apply_one(conn, migration, config)
            applied_results.append(result)
            if logger is not None:
                logger(migration.version, migration.description, result.duration_ms)

        return applied_results
    finally:
        advisory_unlock(conn, MIGRATION_LOCK_KEY)


def run_host_identity() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "unknown"


def _print_status(conn, config) -> int:
    result = status(conn, config)
    if result.checksum_mismatches:
        print(f"CHECKSUM MISMATCH: {', '.join(result.checksum_mismatches)}")
        print("A previously applied migration's referenced file(s) changed. This must be investigated.")
        return 1
    print(f"Applied migrations ({len(result.applied)}):")
    for m in result.applied:
        print(f"  {m.version}  {m.description}  applied_at={m.applied_at.isoformat()}  ({m.duration_ms:.1f} ms)")
    print(f"Pending migrations ({len(result.pending)}):")
    for m in result.pending:
        print(f"  {m.version}  {m.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: `python3 -m tuva_postgres.migrations [--status] [--baseline-existing]`.

    This is what scripts/apply_schema.sh delegates to -- the single
    authoritative schema-deployment path (see docs/RUNBOOK.md).
    """
    import argparse

    from .config import REQUIRE_DB, PipelineConfig

    parser = argparse.ArgumentParser(prog="python -m tuva_postgres.migrations")
    parser.add_argument("--status", action="store_true", help="print migration status and exit (read-only)")
    parser.add_argument(
        "--baseline-existing",
        action="store_true",
        help="explicitly allow baselining migration 0001 against a non-empty, pre-existing schema",
    )
    args = parser.parse_args(argv)

    config = PipelineConfig.load(required=REQUIRE_DB)
    conn = connect(config.pg_dsn)
    try:
        if args.status:
            return _print_status(conn, config)

        applied = apply_pending(
            conn,
            config,
            baseline_existing=args.baseline_existing,
            logger=lambda v, d, ms: print(f"Applied {v}: {d} ({ms:.1f} ms)"),
        )
        if not applied:
            print("No pending migrations. Database is up to date.")
        else:
            print(f"Applied {len(applied)} migration(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    import sys as _sys

    _sys.exit(main())
