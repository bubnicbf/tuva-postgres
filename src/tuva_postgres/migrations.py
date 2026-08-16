"""Migration history and runner.

`db/migrations/` is the sole authoritative home for deployable DDL.
Migrations are discovered from `db/migrations/*.json` manifests (NOT the
`.sql` files directly -- a manifest lists, in a fixed, deterministic
order, which file(s) make up that migration). Every migration owns an
exclusive directory under `db/migrations/sql/`, named after its manifest's
filename (e.g. `0001_baseline.json` -> `db/migrations/sql/0001_baseline/`,
itself organized into `core/`, `views/`, and `terminology/` subdirectories
for readability -- see db/migrations/0001_baseline.json). `discover()`
enforces this: every referenced file must resolve to a regular file
inside the repository (no path traversal), under `db/migrations/sql/`,
and specifically under the directory owned by that exact migration
version -- a new migration can never reference or silently depend on
another migration's already-applied SQL.

Each migration's checksum is a SHA-256 over its ordered constituent
files' contents (framed with path+length so reordering or truncation
changes the hash); a later edit to any file a migration manifest
references is therefore detected as a checksum mismatch on the next run,
exactly like editing an already-applied migration file directly. The
checksum is computed purely from a migration's constituent SQL files --
it never hashes the manifest JSON itself, so adding or changing manifest
metadata (such as `execution`, below) never changes a migration's
checksum.

Execution modes -- every manifest must declare exactly one:
  * "one_time" (see `ExecutionMode.ONE_TIME`): applied at most once. Once
    applied, its SQL, file order, checksum, and execution mode are all
    immutable -- any drift (changed checksum, or a manifest that now
    claims a different execution mode) is a hard error that blocks all
    further migration activity. Migrations 0001 and 0002 are one_time.
  * "repeatable" (see `ExecutionMode.REPEATABLE`): applied on first
    discovery, then transactionally reapplied whenever its checksum
    changes, and skipped otherwise -- standard checksum-driven repeatable-
    migration semantics (think a `CREATE OR REPLACE VIEW`/function you
    want to keep current, not a one-off schema change). A changed
    repeatable migration is *pending work*, not an integrity failure.
    Repeatable SQL must be written idempotently (`CREATE OR REPLACE`,
    `... IF NOT EXISTS`, etc.) -- the runner does not attempt to parse or
    verify SQL idempotency itself.

Ordering: within `apply_pending()`, ALL pending one_time migrations run
first, in ascending version order; only after they all succeed do pending
repeatable migrations (initial application or reapplication) run, also in
ascending version order. This lets a repeatable view or function safely
depend on a schema object a pending one_time migration is about to
create, regardless of how manifests happen to interleave by version
number.

Applied migrations and their file layout are immutable: moving a file
without changing its basename or bytes preserves its checksum (and is
how migrations 0001/0002 were reorganized into version-owned directories
without invalidating databases that already applied them), but editing
content, reordering a manifest's `files` list, or changing which files a
migration includes always changes the checksum.

`schema_migrations` itself is bootstrapped (and additively, idempotently
upgraded for execution-mode tracking) directly by this module -- not
sourced from a migration file -- because it must exist, in the shape the
runner expects, before any migration can be tracked. Everything else
migrations create (pipeline_runs, pipeline_artifacts, ...) is a normal
tracked migration. See `ensure_history_table()` for exactly what the
upgrade does and why it's always safe to (re)run, including against a
pre-execution-mode database.

Planning (classifying discovered migrations against applied history --
pending one_time, pending repeatable [initial or changed], one_time
checksum mismatch, execution-mode mismatch) is a single pure function,
`_plan_status()`, with no database access of its own. `status()` (read-
only) and `apply_pending()` both call it against whatever `applied`
mapping they've already read, so the two paths can never subtly disagree
about what state a migration is in.

Adding a new migration (see docs/RUNBOOK.md "Adding a new migration" for
the full walkthrough): pick the next unused numeric version, add
`db/migrations/{version}_{slug}.json` (declaring `"execution"`) and its
SQL under `db/migrations/{version}_{slug}/`, and never edit an existing,
applied migration's manifest or files -- add a new one instead.

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
from enum import Enum
from pathlib import Path

from . import __version__
from .db import (
    connect,
    qualified_relation,
    quote_ident,
    substitute_psql_vars,
    try_advisory_lock,
    advisory_unlock,
    MIGRATION_LOCK_KEY,
)
from .errors import MigrationError, LockError
from .identifiers import InvalidIdentifierError, validate_identifier
from .manifest import MANAGED_TABLES

_VERSION_RE = re.compile(r"^[0-9]{4,}$")


class ExecutionMode(str, Enum):
    """The exactly-two allowed values of a manifest's required
    `"execution"` field. Subclasses `str` so a mode compares equal to,
    and can be inserted/selected as, its plain string value -- there is
    exactly one representation, never scattered string literals."""

    ONE_TIME = "one_time"
    REPEATABLE = "repeatable"


@dataclass(frozen=True)
class MigrationDef:
    version: str
    description: str
    execution: ExecutionMode
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
    execution: ExecutionMode
    execution_count: int


@dataclass(frozen=True)
class MigrationStatus:
    """A pure classification of every discovered migration against
    applied history -- see `_plan_status()`, the single function that
    produces this (used identically by `status()` and `apply_pending()`).
    """

    applied_one_time: tuple[AppliedMigration, ...]
    applied_repeatable_current: tuple[AppliedMigration, ...]
    pending_one_time: tuple[MigrationDef, ...]
    pending_repeatable_initial: tuple[MigrationDef, ...]
    pending_repeatable_changed: tuple[MigrationDef, ...]
    one_time_mismatches: tuple[str, ...]
    mode_mismatches: tuple[str, ...]

    @property
    def pending(self) -> tuple[MigrationDef, ...]:
        """Every migration `apply_pending()` would act on: pending
        one_time migrations, then repeatable migrations awaiting initial
        application or reapplication (in that priority order -- see
        module docstring "Ordering")."""
        return self.pending_one_time + self.pending_repeatable_initial + self.pending_repeatable_changed

    @property
    def has_integrity_failures(self) -> bool:
        """True if the database is in a state `apply_pending()` will
        refuse to proceed past: a one_time migration whose checksum
        drifted, or any migration whose execution mode no longer matches
        its applied history. A changed *repeatable* migration is deliber-
        ately excluded -- that's pending work, not an integrity failure."""
        return bool(self.one_time_mismatches or self.mode_mismatches)


def _validate_identifier(name: str, value: str) -> str:
    """Validate `value` against the shared identifier policy (see
    identifiers.py), translating a failure into `MigrationError` -- the
    domain error this module's callers already expect from every other
    manifest/history validation problem."""
    try:
        return validate_identifier(value, name)
    except InvalidIdentifierError as exc:
        raise MigrationError(str(exc)) from exc


MIGRATIONS_SQL_DIRNAME = "sql"


def discover(migrations_dir: Path, repo_root: Path) -> list[MigrationDef]:
    manifest_paths = sorted(migrations_dir.glob("*.json"))
    if not manifest_paths:
        raise MigrationError(f"no migration manifests found under {migrations_dir}")

    repo_root = repo_root.resolve()
    sql_root = (migrations_dir / MIGRATIONS_SQL_DIRNAME).resolve()

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
        execution_raw = raw.get("execution")

        if not isinstance(version, str) or not _VERSION_RE.match(version):
            raise MigrationError(f"{manifest_path}: 'version' must be a numeric string like '0001'")
        if not isinstance(description, str) or not description.strip():
            raise MigrationError(f"{manifest_path}: 'description' must be a nonempty string")
        if not isinstance(files, list) or not files:
            raise MigrationError(f"{manifest_path}: 'files' must be a nonempty list")

        # Execution mode is required and must be exactly one of the
        # allowed ExecutionMode values -- never inferred from SQL
        # contents, filename, version, or description, and never
        # silently defaulted. Missing, wrong-typed, and unknown values
        # are all manifest validation errors.
        allowed = ", ".join(repr(m.value) for m in ExecutionMode)
        if not isinstance(execution_raw, str):
            raise MigrationError(
                f"{manifest_path}: 'execution' is required and must be a string, one of {allowed} "
                f"(got {execution_raw!r})"
            )
        try:
            execution = ExecutionMode(execution_raw)
        except ValueError:
            raise MigrationError(
                f"{manifest_path}: 'execution'={execution_raw!r} is not a valid execution mode "
                f"(expected one of {allowed})"
            ) from None

        if version in seen_versions:
            raise MigrationError(
                f"duplicate migration version {version!r}: {seen_versions[version]} and {manifest_path}"
            )
        seen_versions[version] = manifest_path

        # Every migration owns exactly one directory under
        # db/migrations/sql/, named after its own manifest filename (e.g.
        # "0001_baseline.json" -> ".../sql/0001_baseline/"). This is what
        # makes a file's owning migration obvious from its path alone, and
        # stops one migration's manifest from referencing (or silently
        # depending on) another migration's already-applied SQL.
        owned_dir = (sql_root / manifest_path.stem).resolve()

        resolved_files = []
        for rel in files:
            if not isinstance(rel, str) or not rel.strip():
                raise MigrationError(f"{manifest_path}: 'files' entries must be nonempty strings")

            path = (repo_root / rel).resolve()

            # Path traversal safety: a manifest must never be able to
            # reference a file outside the repository, regardless of how
            # many "../" segments or absolute-looking paths it contains.
            if not path.is_relative_to(repo_root):
                raise MigrationError(
                    f"{manifest_path}: referenced file {rel!r} resolves outside the repository "
                    f"root ({path}) -- refusing to load it"
                )

            if not path.is_file():
                raise MigrationError(f"{manifest_path}: referenced file not found: {rel}")

            # db/migrations/ is the sole authoritative home for deployable
            # DDL: every referenced file must live under db/migrations/sql/
            # (not a separate, mutable directory like the retired
            # db/tables/), and specifically inside the directory owned by
            # this exact migration version.
            if not path.is_relative_to(sql_root):
                raise MigrationError(
                    f"{manifest_path}: referenced file {rel!r} is not under "
                    f"{sql_root.relative_to(repo_root)}/ -- deployable migration SQL must live under "
                    "db/migrations/sql/, never a separate mutable directory"
                )
            if not path.is_relative_to(owned_dir):
                raise MigrationError(
                    f"{manifest_path}: referenced file {rel!r} is outside this migration's own "
                    f"directory ({owned_dir.relative_to(repo_root)}/) -- each migration's SQL must "
                    "live under the directory owned by its version so ownership is obvious from the "
                    "path alone; add a new migration instead of reaching into another one's directory"
                )

            resolved_files.append(path)

        migrations.append(
            MigrationDef(
                version=version,
                description=description,
                execution=execution,
                files=tuple(resolved_files),
                var_map=dict(var_map),
                manifest_path=manifest_path,
            )
        )

    migrations.sort(key=lambda m: m.version)
    return migrations


def compute_checksum(migration: MigrationDef) -> str:
    """SHA-256 over `migration`'s ordered constituent SQL files only --
    basename, byte length, and content, framed so reordering or
    truncation changes the hash. Deliberately independent of the
    manifest's own fields (version/description/execution/vars): adding or
    changing manifest metadata never changes a migration's checksum."""
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
    """Bootstrap (and idempotently upgrade) `<ops_schema>.schema_migrations`.

    `schema_migrations` is deliberately bootstrap-managed here rather than
    by a normal tracked migration -- it must exist, in the shape this
    runner expects, before any migration can be tracked.

    The upgrade from the original six-column shape (version, description,
    checksum, applied_at, duration_ms, app_version) to the execution-mode-
    aware shape (+ execution, execution_count) is purely additive and
    idempotent:
      * `ADD COLUMN IF NOT EXISTS` -- a no-op against a table that already
        has these columns (including a brand-new table, or one this
        function has already upgraded).
      * The backfill (`UPDATE ... WHERE execution IS NULL`) only ever
        touches rows that predate execution-mode tracking entirely, and
        only ever sets `execution`/`execution_count` -- it never modifies
        an existing row's version, checksum, applied_at, description, or
        duration_ms/app_version. Pre-existing rows are backfilled as
        `ExecutionMode.ONE_TIME` with `execution_count = 1`, matching this
        repository's compatibility rule that migrations applied before
        execution-mode tracking existed are one_time.
      * The `NOT NULL`/`CHECK` enforcement is (re)applied every call, but
        is harmless against an already-conforming table (schema_migrations
        has, at most, one row per migration -- there is no realistic scale
        concern here); the `CHECK` constraint is added only if it doesn't
        already exist -- checked via an ordinary parameterized catalog
        query (never a schema name spliced into a `DO $$ ... $$` block as
        raw text), then a separately composed `ALTER TABLE ... ADD
        CONSTRAINT` -- so this function is safe to call every time (every
        `apply_pending()` invocation does).

    This function is called only from `apply_pending()` -- never from the
    read-only `status()` path (see its docstring for why: status() must
    work against an un-upgraded, pre-execution-mode table without ever
    mutating it).
    """
    ops_schema = _validate_identifier("OPS_SCHEMA", ops_schema)
    schema_ident = quote_ident(ops_schema)
    relation = qualified_relation(ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")

    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_ident}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {relation} (
              version      text PRIMARY KEY,
              description  text NOT NULL,
              checksum     text NOT NULL,
              applied_at   timestamptz NOT NULL,
              duration_ms  double precision NOT NULL,
              app_version  text NOT NULL
            )
            """
        )

        # --- Additive, idempotent upgrade for execution-mode tracking ---
        cur.execute(f"ALTER TABLE {relation} ADD COLUMN IF NOT EXISTS execution text")
        cur.execute(f"ALTER TABLE {relation} ADD COLUMN IF NOT EXISTS execution_count integer")
        # Backfill only rows that predate execution-mode tracking. Never
        # touches version/checksum/applied_at/description/duration_ms/
        # app_version. The backfilled value is an ordinary data value
        # (ExecutionMode.ONE_TIME.value -- a fixed Python constant, never
        # user input either way), bound as a normal parameter rather than
        # spliced into the SQL text.
        cur.execute(f"UPDATE {relation} SET execution = %s WHERE execution IS NULL", (ExecutionMode.ONE_TIME.value,))
        cur.execute(f"UPDATE {relation} SET execution_count = %s WHERE execution_count IS NULL", (1,))
        # DDL default/check-constraint bodies must be constant expressions
        # -- PostgreSQL does not accept a bind parameter here the way a
        # normal DML statement would, so these two values are embedded
        # directly. Both are safe: `ExecutionMode` members are a fixed,
        # hardcoded two-value Python enum (never derived from user input,
        # a manifest, or any environment variable), not the dynamic
        # `ops_schema` identifier this function exists to protect.
        cur.execute(f"ALTER TABLE {relation} ALTER COLUMN execution SET DEFAULT '{ExecutionMode.ONE_TIME.value}'")
        cur.execute(f"ALTER TABLE {relation} ALTER COLUMN execution SET NOT NULL")
        cur.execute(f"ALTER TABLE {relation} ALTER COLUMN execution_count SET DEFAULT 1")
        cur.execute(f"ALTER TABLE {relation} ALTER COLUMN execution_count SET NOT NULL")

        # Add the CHECK constraint only if it doesn't already exist --
        # PostgreSQL has no "ADD CONSTRAINT IF NOT EXISTS". The existence
        # check is a plain parameterized catalog query (ops_schema is a
        # bound value here, not SQL syntax); the ALTER TABLE that follows
        # is the same validated, composed `relation` used throughout this
        # function -- at no point is `ops_schema` spliced into a
        # PL/pgSQL DO block as raw text.
        cur.execute(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class r ON r.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = r.relnamespace "
            "WHERE n.nspname = %s AND r.relname = %s AND c.conname = %s",
            (ops_schema, "schema_migrations", "schema_migrations_execution_check"),
        )
        constraint_exists = cur.fetchone() is not None
        if not constraint_exists:
            allowed_values = ", ".join(f"'{m.value}'" for m in ExecutionMode)  # trusted enum constants, not user input
            cur.execute(
                f"ALTER TABLE {relation} ADD CONSTRAINT schema_migrations_execution_check "
                f"CHECK (execution IN ({allowed_values}))"
            )
    conn.commit()


def _history_table_exists(conn, ops_schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'schema_migrations'",
            (ops_schema,),
        )
        return cur.fetchone() is not None


def _history_has_execution_columns(conn, ops_schema: str) -> bool:
    """Read-only (information_schema only) check for whether the
    execution-mode columns have been added yet. Lets `status()` read
    both the old (pre-upgrade) and new schema_migrations shapes without
    ever running the (mutating) upgrade DDL in `ensure_history_table()`
    itself."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'schema_migrations' AND column_name = 'execution'",
            (ops_schema,),
        )
        return cur.fetchone() is not None


def _read_applied(conn, ops_schema: str) -> dict[str, AppliedMigration]:
    """Read every recorded migration. Works against both the old
    (six-column) and new (execution-mode-aware) schema_migrations shapes
    -- see `_history_has_execution_columns()`. Rows from an old-shaped
    table (or, defensively, any row with a NULL execution/execution_count
    that predates the backfill) are interpreted as `ExecutionMode.ONE_TIME`
    with `execution_count=1`, per this repository's compatibility rule."""
    applied: dict[str, AppliedMigration] = {}
    ops_schema = _validate_identifier("OPS_SCHEMA", ops_schema)
    relation = qualified_relation(ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")
    has_execution_columns = _history_has_execution_columns(conn, ops_schema)
    with conn.cursor() as cur:
        if has_execution_columns:
            cur.execute(
                f"SELECT version, description, checksum, applied_at, duration_ms, app_version, "
                f"execution, execution_count "
                f"FROM {relation} ORDER BY version"
            )
            rows = [
                (version, description, checksum, applied_at, duration_ms, app_version, execution, execution_count)
                for version, description, checksum, applied_at, duration_ms, app_version, execution, execution_count
                in cur.fetchall()
            ]
        else:
            cur.execute(
                f"SELECT version, description, checksum, applied_at, duration_ms, app_version "
                f"FROM {relation} ORDER BY version"
            )
            rows = [
                (version, description, checksum, applied_at, duration_ms, app_version, None, None)
                for version, description, checksum, applied_at, duration_ms, app_version in cur.fetchall()
            ]

        for version, description, checksum, applied_at, duration_ms, app_version, execution, execution_count in rows:
            applied[version] = AppliedMigration(
                version=version,
                description=description,
                checksum=checksum,
                applied_at=applied_at,
                duration_ms=duration_ms,
                app_version=app_version,
                execution=ExecutionMode(execution) if execution else ExecutionMode.ONE_TIME,
                execution_count=execution_count if execution_count is not None else 1,
            )
    return applied


def _plan_status(
    all_migrations: list[MigrationDef], applied: dict[str, AppliedMigration]
) -> MigrationStatus:
    """Pure classification of discovered migrations against already-
    applied history. Touches no database -- `status()` and
    `apply_pending()` both call this against whatever `applied` mapping
    they've already read, so the two paths can never subtly disagree
    about what state a migration is in.

    `all_migrations` is assumed pre-sorted ascending by version (as
    `discover()` returns it), so every output tuple below is also in
    ascending version order.
    """
    applied_one_time: list[AppliedMigration] = []
    applied_repeatable_current: list[AppliedMigration] = []
    pending_one_time: list[MigrationDef] = []
    pending_repeatable_initial: list[MigrationDef] = []
    pending_repeatable_changed: list[MigrationDef] = []
    one_time_mismatches: list[str] = []
    mode_mismatches: list[str] = []

    for migration in all_migrations:
        record = applied.get(migration.version)

        if record is None:
            if migration.execution is ExecutionMode.ONE_TIME:
                pending_one_time.append(migration)
            else:
                pending_repeatable_initial.append(migration)
            continue

        # Execution mode is immutable once applied -- checked before
        # checksum, since a mode change is always a configuration/
        # integrity problem regardless of whether the SQL also changed.
        if record.execution != migration.execution:
            mode_mismatches.append(migration.version)
            continue

        checksum = compute_checksum(migration)
        if migration.execution is ExecutionMode.ONE_TIME:
            if record.checksum == checksum:
                applied_one_time.append(record)
            else:
                one_time_mismatches.append(migration.version)
        else:
            if record.checksum == checksum:
                applied_repeatable_current.append(record)
            else:
                # A changed repeatable migration is pending work, not a
                # checksum-integrity failure.
                pending_repeatable_changed.append(migration)

    return MigrationStatus(
        applied_one_time=tuple(applied_one_time),
        applied_repeatable_current=tuple(applied_repeatable_current),
        pending_one_time=tuple(pending_one_time),
        pending_repeatable_initial=tuple(pending_repeatable_initial),
        pending_repeatable_changed=tuple(pending_repeatable_changed),
        one_time_mismatches=tuple(one_time_mismatches),
        mode_mismatches=tuple(mode_mismatches),
    )


def _default_migrations_and_repo_root(
    migrations_dir: Path | None, repo_root: Path | None
) -> tuple[Path, Path]:
    if migrations_dir is None:
        migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations"
    if repo_root is None:
        repo_root = migrations_dir.parents[1]
    return migrations_dir, repo_root


def status(
    conn, config, *, migrations_dir: Path | None = None, repo_root: Path | None = None
) -> MigrationStatus:
    """Read-only: never writes, never takes a lock, and never runs the
    schema_migrations metadata upgrade in `ensure_history_table()` --
    safe to call against a database whose history table predates
    execution-mode tracking entirely (its rows are interpreted as
    one_time/execution_count=1, per `_read_applied()`) as well as one
    that's already been upgraded. Safe to run anytime, including
    concurrently with an in-progress migration.

    `migrations_dir`/`repo_root` are an injectable seam (defaulting to
    this repository's real db/migrations/) so callers -- tests, in
    particular -- can point discovery at an isolated fixture set without
    touching the real, committed migrations.

    Validates `config.ops_schema` against the shared identifier policy
    before running any query -- defense in depth alongside `PipelineConfig
    .load()`'s own validation, since `_history_table_exists()` only ever
    binds `ops_schema` as an ordinary parameterized value (safe from
    injection either way, but not itself a policy check), and a caller
    could in principle construct a config object directly rather than
    through `PipelineConfig.load()`. Raises before touching the database
    (never mutates anything either way -- this function is read-only).
    """
    _validate_identifier("OPS_SCHEMA", config.ops_schema)

    migrations_dir, repo_root = _default_migrations_and_repo_root(migrations_dir, repo_root)
    all_migrations = discover(migrations_dir, repo_root)

    if not _history_table_exists(conn, config.ops_schema):
        return _plan_status(all_migrations, {})

    applied = _read_applied(conn, config.ops_schema)
    return _plan_status(all_migrations, applied)


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


def _apply_one_time(conn, migration: MigrationDef, config) -> AppliedMigration:
    """Execute a one_time migration's SQL and INSERT its history row in
    one transaction. Only ever called for a migration with no existing
    history row (see `_plan_status()`'s `pending_one_time`) -- a one_time
    history row, once inserted, is never updated by this runner."""
    variables = _resolve_vars(migration, config)
    sql_text = _rendered_sql(migration, variables)
    checksum = compute_checksum(migration)
    relation = qualified_relation(config.ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")
    started = time.monotonic()
    applied_at = datetime.now(timezone.utc)

    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            duration_ms = (time.monotonic() - started) * 1000.0
            cur.execute(
                f"INSERT INTO {relation} "
                f"(version, description, checksum, applied_at, duration_ms, app_version, "
                f"execution, execution_count) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    migration.version,
                    migration.description,
                    checksum,
                    applied_at,
                    duration_ms,
                    __version__,
                    ExecutionMode.ONE_TIME.value,
                    1,
                ),
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
        execution=ExecutionMode.ONE_TIME,
        execution_count=1,
    )


def _apply_repeatable(conn, migration: MigrationDef, config, *, prior_execution_count: int) -> AppliedMigration:
    """Execute a repeatable migration's SQL and INSERT-or-update its
    history row in one transaction (`ON CONFLICT (version) DO UPDATE`
    handles both initial application and reapplication with a single
    statement). If the SQL fails, the whole transaction (including any
    partial history write) rolls back and the previously successful
    history row -- checksum, description, applied_at, duration, app
    version, and execution_count -- is left completely untouched, exactly
    as if this call had never happened.
    """
    variables = _resolve_vars(migration, config)
    sql_text = _rendered_sql(migration, variables)
    checksum = compute_checksum(migration)
    relation = qualified_relation(config.ops_schema, "schema_migrations", schema_label="OPS_SCHEMA")
    started = time.monotonic()
    applied_at = datetime.now(timezone.utc)
    new_execution_count = prior_execution_count + 1

    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            duration_ms = (time.monotonic() - started) * 1000.0
            cur.execute(
                f"INSERT INTO {relation} "
                f"(version, description, checksum, applied_at, duration_ms, app_version, "
                f"execution, execution_count) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (version) DO UPDATE SET "
                f"description = EXCLUDED.description, "
                f"checksum = EXCLUDED.checksum, "
                f"applied_at = EXCLUDED.applied_at, "
                f"duration_ms = EXCLUDED.duration_ms, "
                f"app_version = EXCLUDED.app_version, "
                f"execution = EXCLUDED.execution, "
                f"execution_count = EXCLUDED.execution_count",
                (
                    migration.version,
                    migration.description,
                    checksum,
                    applied_at,
                    duration_ms,
                    __version__,
                    ExecutionMode.REPEATABLE.value,
                    new_execution_count,
                ),
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
        execution=ExecutionMode.REPEATABLE,
        execution_count=new_execution_count,
    )


def apply_pending(
    conn,
    config,
    *,
    baseline_existing: bool = False,
    logger=None,
    migrations_dir: Path | None = None,
    repo_root: Path | None = None,
) -> list[AppliedMigration]:
    """Apply every pending migration, in order: all pending one_time
    migrations (ascending version), then all repeatable migrations
    awaiting initial application or reapplication (ascending version) --
    see module docstring "Ordering". Refuses to proceed at all (before
    executing any SQL) if any applied migration shows a one_time checksum
    mismatch or an execution-mode mismatch -- migrations are immutable
    once applied; add a new migration instead of editing one.

    `migrations_dir`/`repo_root`: see `status()`'s docstring -- the same
    injectable seam, for tests.
    """
    migrations_dir, repo_root = _default_migrations_and_repo_root(migrations_dir, repo_root)
    all_migrations = discover(migrations_dir, repo_root)

    if not try_advisory_lock(conn, MIGRATION_LOCK_KEY):
        raise LockError(
            "could not acquire the PostgreSQL migration advisory lock -- another migration run "
            "appears to be in progress"
        )
    try:
        ensure_history_table(conn, config.ops_schema)
        applied = _read_applied(conn, config.ops_schema)
        plan = _plan_status(all_migrations, applied)

        if plan.mode_mismatches:
            raise MigrationError(
                f"migration(s) {', '.join(plan.mode_mismatches)} declare a different execution mode than "
                "when they were applied. Execution mode is immutable once a migration is applied -- add a "
                "new migration instead of changing this one's mode."
            )
        if plan.one_time_mismatches:
            raise MigrationError(
                f"one_time migration(s) {', '.join(plan.one_time_mismatches)} have changed since they were "
                "applied -- checksum mismatch. One-time migrations are immutable once applied; add a new "
                "migration instead of editing this one."
            )

        first_version = all_migrations[0].version if all_migrations else None
        applied_results: list[AppliedMigration] = []

        # --- Pass 1: all pending one_time migrations, ascending version -
        for migration in plan.pending_one_time:
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

            result = _apply_one_time(conn, migration, config)
            applied_results.append(result)
            if logger is not None:
                logger(migration.version, migration.description, result.duration_ms)

        # --- Pass 2: repeatable migrations (initial + changed), only
        #     after every pending one_time migration above has succeeded,
        #     ascending version ------------------------------------------
        to_apply_repeatable = tuple(
            sorted(plan.pending_repeatable_initial + plan.pending_repeatable_changed, key=lambda m: m.version)
        )
        for migration in to_apply_repeatable:
            prior = applied.get(migration.version)
            result = _apply_repeatable(
                conn, migration, config, prior_execution_count=(prior.execution_count if prior else 0)
            )
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
    if result.has_integrity_failures:
        if result.one_time_mismatches:
            print(f"ONE-TIME CHECKSUM MISMATCH: {', '.join(result.one_time_mismatches)}")
            print(
                "A previously applied one-time migration's referenced file(s) changed. "
                "This must be investigated -- one-time migrations are immutable once applied."
            )
        if result.mode_mismatches:
            print(f"EXECUTION MODE MISMATCH: {', '.join(result.mode_mismatches)}")
            print(
                "A migration's manifest 'execution' no longer matches its applied history. "
                "Execution mode is immutable once a migration is applied."
            )
        return 1

    print(f"Applied one-time migrations ({len(result.applied_one_time)}):")
    for m in result.applied_one_time:
        print(f"  {m.version}  {m.description}  applied_at={m.applied_at.isoformat()}  ({m.duration_ms:.1f} ms)")

    print(f"Current repeatable migrations ({len(result.applied_repeatable_current)}):")
    for m in result.applied_repeatable_current:
        print(
            f"  {m.version}  {m.description}  applied_at={m.applied_at.isoformat()}  "
            f"execution_count={m.execution_count}"
        )

    print(f"Pending one-time migrations ({len(result.pending_one_time)}):")
    for m in result.pending_one_time:
        print(f"  {m.version}  {m.description}")

    print(f"Repeatable migrations awaiting initial application ({len(result.pending_repeatable_initial)}):")
    for m in result.pending_repeatable_initial:
        print(f"  {m.version}  {m.description}")

    print(
        f"Repeatable migrations awaiting reapplication, checksum changed "
        f"({len(result.pending_repeatable_changed)}):"
    )
    for m in result.pending_repeatable_changed:
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
