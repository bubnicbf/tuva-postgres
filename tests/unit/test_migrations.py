"""Standard-library unit tests for the migration-discovery/checksum logic
in tuva_postgres.migrations that don't require a real PostgreSQL
connection (see tests/integration/test_pipeline_integration.py for the
disposable-database coverage of apply_pending/status).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.db import substitute_psql_vars  # noqa: E402
from tuva_postgres.errors import MigrationError  # noqa: E402
from tuva_postgres.migrations import (  # noqa: E402
    AppliedMigration,
    ExecutionMode,
    compute_checksum,
    discover,
    _history_has_execution_columns,
    _plan_status,
    _read_applied,
)

# Pinned pre-refactor checksums for the real, committed migrations 0001 and
# 0002 -- captured with this same compute_checksum implementation against
# db/tables/*.sql / db/migrations/sql/0002_operational_schema.sql before
# those files were reorganized into version-owned directories under
# db/migrations/sql/. These must never change: a database that already
# recorded these migrations as applied must never see a checksum mismatch.
REAL_0001_CHECKSUM = "6a7cfe125ac4becc4e18000ced22530394ed0d5bda0b7898928820bce83a0445"
REAL_0002_CHECKSUM = "fd8a57293ec70b8f78a9347ec2eba571d5b375100cd665c733dc2d536397a2e4"


class TestSubstitutePsqlVars(unittest.TestCase):
    def test_identifier_form(self):
        out = substitute_psql_vars('ALTER TABLE :"schema".patient ...', {"schema": "tuva"})
        self.assertEqual(out, 'ALTER TABLE "tuva".patient ...')

    def test_string_literal_form(self):
        out = substitute_psql_vars("WHERE n.nspname = :'schema'", {"schema": "tuva"})
        self.assertEqual(out, "WHERE n.nspname = 'tuva'")

    def test_embedded_quotes_are_escaped(self):
        out = substitute_psql_vars(':"schema"', {"schema": 'weird"name'})
        self.assertEqual(out, '"weird""name"')

    def test_does_not_touch_type_casts(self):
        # `::date` etc. must never be mistaken for a psql variable.
        out = substitute_psql_vars("value::date", {"schema": "tuva"})
        self.assertEqual(out, "value::date")


class TestDiscoverAndChecksum(unittest.TestCase):
    """Synthetic fixtures use the same layout convention enforced by
    discover() and used by the real repository: every migration's SQL
    lives under db/migrations/sql/<manifest-filename-stem>/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.migrations_dir = self.repo_root / "db" / "migrations"
        self.migrations_dir.mkdir(parents=True)

    def _write_sql(self, subdir: str, name: str, content: str) -> Path:
        d = self.migrations_dir / "sql" / subdir
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text(content, encoding="utf-8")
        return path

    def _write_manifest(
        self,
        filename: str,
        version: str,
        files: list[str],
        description="test migration",
        execution="one_time",
        _omit_execution=False,
        _execution_override=None,
    ):
        manifest = {"version": version, "description": description, "files": files, "vars": {}}
        if _omit_execution:
            pass  # deliberately leave 'execution' out, for validation-error tests
        elif _execution_override is not None:
            manifest["execution"] = _execution_override  # deliberately invalid value/type
        else:
            manifest["execution"] = execution
        (self.migrations_dir / filename).write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    def test_real_repo_migrations_discover_without_error(self):
        # Exercises the actual, committed db/migrations/*.json against the
        # actual repo -- not a synthetic fixture -- so a broken manifest
        # (missing file, bad version, duplicate, out-of-layout reference)
        # fails this test directly.
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        migrations = discover(real_migrations_dir, REPO_ROOT)
        self.assertGreaterEqual(len(migrations), 2)
        self.assertEqual([m.version for m in migrations], sorted(m.version for m in migrations))
        for migration in migrations:
            checksum = compute_checksum(migration)
            self.assertEqual(len(checksum), 64)

    def test_real_repo_migration_0001_checksum_matches_pre_refactor_value(self):
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        by_version = {m.version: m for m in discover(real_migrations_dir, REPO_ROOT)}
        self.assertIn("0001", by_version)
        self.assertEqual(compute_checksum(by_version["0001"]), REAL_0001_CHECKSUM)

    def test_real_repo_migration_0002_checksum_matches_pre_refactor_value(self):
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        by_version = {m.version: m for m in discover(real_migrations_dir, REPO_ROOT)}
        self.assertIn("0002", by_version)
        self.assertEqual(compute_checksum(by_version["0002"]), REAL_0002_CHECKSUM)

    def test_real_repo_no_manifest_references_db_tables(self):
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        for manifest_path in sorted(real_migrations_dir.glob("*.json")):
            raw = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("db/tables", raw, f"{manifest_path.name} still references db/tables/")

    def test_deterministic_ordering(self):
        self._write_sql("0002_b", "b.sql", "SELECT 1;")
        self._write_manifest("0002_b.json", "0002", ["db/migrations/sql/0002_b/b.sql"])
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])

        migrations = discover(self.migrations_dir, self.repo_root)
        self.assertEqual([m.version for m in migrations], ["0001", "0002"])

    def test_duplicate_version_rejected(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        self._write_sql("0001_b", "b.sql", "SELECT 1;")
        self._write_manifest("0001_b.json", "0001", ["db/migrations/sql/0001_b/b.sql"])

        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_missing_referenced_file_rejected(self):
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/does_not_exist.sql"])
        with self.assertRaises(MigrationError):
            discover(self.migrations_dir, self.repo_root)

    def test_path_traversal_rejected(self):
        # A manifest must never be able to reference a file outside the
        # repository, regardless of how many "../" segments it uses.
        self._write_manifest("0001_a.json", "0001", ["../outside.sql"])
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("outside the repository", str(ctx.exception).lower())

    def test_file_outside_migrations_sql_dir_rejected(self):
        # Deployable migration SQL must live under db/migrations/sql/, not
        # some other, mutable directory (like the retired db/tables/).
        other_dir = self.repo_root / "other"
        other_dir.mkdir()
        (other_dir / "file.sql").write_text("SELECT 1;", encoding="utf-8")
        self._write_manifest("0001_a.json", "0001", ["other/file.sql"])
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("db/migrations/sql", str(ctx.exception))

    def test_file_outside_owning_migration_directory_rejected(self):
        # A migration must not be able to reference another migration's
        # version-owned directory.
        self._write_sql("0002_b", "b.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0002_b/b.sql"])
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("own", str(ctx.exception).lower())

    def test_checksum_changes_when_file_content_changes(self):
        path = self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        migrations = discover(self.migrations_dir, self.repo_root)
        checksum_before = compute_checksum(migrations[0])

        path.write_text("SELECT 2;", encoding="utf-8")
        migrations_after = discover(self.migrations_dir, self.repo_root)
        checksum_after = compute_checksum(migrations_after[0])

        self.assertNotEqual(checksum_before, checksum_after)

    def test_checksum_stable_for_unchanged_files(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        m1 = discover(self.migrations_dir, self.repo_root)[0]
        m2 = discover(self.migrations_dir, self.repo_root)[0]
        self.assertEqual(compute_checksum(m1), compute_checksum(m2))

    def test_reordering_files_changes_checksum(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_sql("0001_a", "b.sql", "SELECT 2;")
        self._write_manifest(
            "0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql", "db/migrations/sql/0001_a/b.sql"]
        )
        checksum_ab = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])

        self._write_manifest(
            "0001_a.json", "0001", ["db/migrations/sql/0001_a/b.sql", "db/migrations/sql/0001_a/a.sql"]
        )
        checksum_ba = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])

        self.assertNotEqual(checksum_ab, checksum_ba)

    def test_moving_unchanged_file_preserves_checksum(self):
        # Moving a file without changing its basename or bytes must not
        # change its migration's checksum -- this is exactly how the real
        # 0001/0002 migrations were reorganized into version-owned
        # directories without invalidating already-applied databases.
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        checksum_before = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])

        new_dir = self.migrations_dir / "sql" / "0001_renamed"
        new_dir.mkdir(parents=True)
        (self.migrations_dir / "sql" / "0001_a" / "a.sql").rename(new_dir / "a.sql")
        (self.migrations_dir / "0001_a.json").unlink()
        self._write_manifest("0001_renamed.json", "0001", ["db/migrations/sql/0001_renamed/a.sql"])

        checksum_after = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])
        self.assertEqual(checksum_before, checksum_after)


class TestManifestExecutionValidation(unittest.TestCase):
    """discover() must require an explicit, valid 'execution' field on
    every manifest -- never inferred from filename/content/version/
    description, and never silently defaulted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.migrations_dir = self.repo_root / "db" / "migrations"
        self.migrations_dir.mkdir(parents=True)

    def _write_sql(self, subdir: str, name: str, content: str) -> Path:
        d = self.migrations_dir / "sql" / subdir
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text(content, encoding="utf-8")
        return path

    def _write_raw_manifest(self, filename: str, manifest: dict) -> None:
        (self.migrations_dir / filename).write_text(json.dumps(manifest), encoding="utf-8")

    def test_missing_execution_field_rejected(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_raw_manifest(
            "0001_a.json",
            {
                "version": "0001",
                "description": "test",
                "files": ["db/migrations/sql/0001_a/a.sql"],
                "vars": {},
                # no 'execution' key at all
            },
        )
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        msg = str(ctx.exception).lower()
        self.assertIn("execution", msg)
        self.assertIn("0001_a.json", str(ctx.exception))

    def test_execution_wrong_type_rejected(self):
        for bad_value in (None, 1, True, ["one_time"], {"mode": "one_time"}):
            with self.subTest(bad_value=bad_value):
                self._write_sql("0001_a", "a.sql", "SELECT 1;")
                self._write_raw_manifest(
                    "0001_a.json",
                    {
                        "version": "0001",
                        "description": "test",
                        "files": ["db/migrations/sql/0001_a/a.sql"],
                        "vars": {},
                        "execution": bad_value,
                    },
                )
                with self.assertRaises(MigrationError) as ctx:
                    discover(self.migrations_dir, self.repo_root)
                self.assertIn("execution", str(ctx.exception).lower())

    def test_execution_unknown_string_value_rejected(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_raw_manifest(
            "0001_a.json",
            {
                "version": "0001",
                "description": "test",
                "files": ["db/migrations/sql/0001_a/a.sql"],
                "vars": {},
                "execution": "sometimes",
            },
        )
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        msg = str(ctx.exception).lower()
        self.assertIn("execution", msg)
        self.assertIn("sometimes", str(ctx.exception))

    def test_execution_one_time_and_repeatable_both_accepted(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_raw_manifest(
            "0001_a.json",
            {
                "version": "0001",
                "description": "test",
                "files": ["db/migrations/sql/0001_a/a.sql"],
                "vars": {},
                "execution": "one_time",
            },
        )
        self._write_sql("0002_b", "b.sql", "SELECT 1;")
        self._write_raw_manifest(
            "0002_b.json",
            {
                "version": "0002",
                "description": "test",
                "files": ["db/migrations/sql/0002_b/b.sql"],
                "vars": {},
                "execution": "repeatable",
            },
        )
        migs = discover(self.migrations_dir, self.repo_root)
        by_version = {m.version: m for m in migs}
        self.assertEqual(by_version["0001"].execution, ExecutionMode.ONE_TIME)
        self.assertEqual(by_version["0002"].execution, ExecutionMode.REPEATABLE)

    def test_execution_mode_not_inferred_from_filename_or_description(self):
        # A manifest named/described as if it were repeatable, but
        # explicitly declaring one_time, must be treated as one_time --
        # mode comes only from the 'execution' field.
        self._write_sql("0001_repeatable_view", "repeatable_view.sql", "CREATE VIEW v AS SELECT 1;")
        self._write_raw_manifest(
            "0001_repeatable_view.json",
            {
                "version": "0001",
                "description": "a repeatable view definition",
                "files": ["db/migrations/sql/0001_repeatable_view/repeatable_view.sql"],
                "vars": {},
                "execution": "one_time",
            },
        )
        migs = discover(self.migrations_dir, self.repo_root)
        self.assertEqual(migs[0].execution, ExecutionMode.ONE_TIME)


class TestChecksumIndependentOfManifestMetadata(unittest.TestCase):
    """compute_checksum() hashes only a migration's constituent SQL
    files -- manifest metadata (execution, description, vars) must never
    change a migration's checksum, since real, already-applied manifests
    are getting an 'execution' field added without touching their SQL."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.migrations_dir = self.repo_root / "db" / "migrations"
        self.migrations_dir.mkdir(parents=True)
        d = self.migrations_dir / "sql" / "0001_a"
        d.mkdir(parents=True)
        (d / "a.sql").write_text("SELECT 1;", encoding="utf-8")

    def _checksum_with(self, description: str, execution: str, vars_: dict) -> str:
        (self.migrations_dir / "0001_a.json").write_text(
            json.dumps(
                {
                    "version": "0001",
                    "description": description,
                    "files": ["db/migrations/sql/0001_a/a.sql"],
                    "vars": vars_,
                    "execution": execution,
                }
            ),
            encoding="utf-8",
        )
        migs = discover(self.migrations_dir, self.repo_root)
        return compute_checksum(migs[0])

    def test_checksum_unaffected_by_execution_field(self):
        checksum_one_time = self._checksum_with("desc", "one_time", {})
        checksum_repeatable = self._checksum_with("desc", "repeatable", {})
        self.assertEqual(checksum_one_time, checksum_repeatable)

    def test_checksum_unaffected_by_description_change(self):
        checksum_a = self._checksum_with("original description", "one_time", {})
        checksum_b = self._checksum_with("a totally different description", "one_time", {})
        self.assertEqual(checksum_a, checksum_b)

    def test_checksum_unaffected_by_vars_change(self):
        checksum_a = self._checksum_with("desc", "one_time", {})
        checksum_b = self._checksum_with("desc", "one_time", {"schema": "PG_SCHEMA"})
        self.assertEqual(checksum_a, checksum_b)


class TestPlanStatus(unittest.TestCase):
    """Pure, database-free tests of _plan_status()'s classification logic
    -- the single function status() and apply_pending() both use, so every
    state transition it recognizes is covered here directly.

    MigrationDef instances still need real backing files (compute_checksum
    reads them), so each test builds a tiny fixture set via discover();
    'applied' history is then fabricated directly as AppliedMigration
    instances, with no database involved anywhere in this class.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.migrations_dir = self.repo_root / "db" / "migrations"
        self.migrations_dir.mkdir(parents=True)

    def _migration(self, version: str, execution: str, content: str = "SELECT 1;"):
        slug = f"{version}_m"
        d = self.migrations_dir / "sql" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "m.sql").write_text(content, encoding="utf-8")
        (self.migrations_dir / f"{slug}.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "description": f"migration {version}",
                    "files": [f"db/migrations/sql/{slug}/m.sql"],
                    "vars": {},
                    "execution": execution,
                }
            ),
            encoding="utf-8",
        )

    def _discover(self):
        return discover(self.migrations_dir, self.repo_root)

    def _applied(self, migration, *, checksum=None, execution=None, execution_count=1) -> AppliedMigration:
        return AppliedMigration(
            version=migration.version,
            description=migration.description,
            checksum=checksum if checksum is not None else compute_checksum(migration),
            applied_at=datetime.now(timezone.utc),
            duration_ms=1.0,
            app_version="test",
            execution=execution if execution is not None else migration.execution,
            execution_count=execution_count,
        )

    def test_never_applied_one_time_is_pending_one_time(self):
        self._migration("0001", "one_time")
        migs = self._discover()
        plan = _plan_status(migs, {})
        self.assertEqual([m.version for m in plan.pending_one_time], ["0001"])
        self.assertEqual(plan.pending_repeatable_initial, ())
        self.assertFalse(plan.has_integrity_failures)

    def test_never_applied_repeatable_is_pending_repeatable_initial(self):
        self._migration("0001", "repeatable")
        migs = self._discover()
        plan = _plan_status(migs, {})
        self.assertEqual([m.version for m in plan.pending_repeatable_initial], ["0001"])
        self.assertEqual(plan.pending_one_time, ())
        self.assertFalse(plan.has_integrity_failures)

    def test_applied_one_time_matching_checksum_is_applied_one_time(self):
        self._migration("0001", "one_time")
        migs = self._discover()
        applied = {"0001": self._applied(migs[0])}
        plan = _plan_status(migs, applied)
        self.assertEqual([m.version for m in plan.applied_one_time], ["0001"])
        self.assertEqual(plan.pending, ())
        self.assertFalse(plan.has_integrity_failures)

    def test_applied_repeatable_matching_checksum_is_applied_repeatable_current(self):
        self._migration("0001", "repeatable")
        migs = self._discover()
        applied = {"0001": self._applied(migs[0])}
        plan = _plan_status(migs, applied)
        self.assertEqual([m.version for m in plan.applied_repeatable_current], ["0001"])
        self.assertEqual(plan.pending, ())
        self.assertFalse(plan.has_integrity_failures)

    def test_one_time_checksum_drift_is_integrity_failure_not_pending(self):
        self._migration("0001", "one_time")
        migs = self._discover()
        applied = {"0001": self._applied(migs[0], checksum="deadbeef" * 8)}
        plan = _plan_status(migs, applied)
        self.assertEqual(plan.one_time_mismatches, ("0001",))
        self.assertEqual(plan.pending, ())  # a mismatch is not "pending work"
        self.assertTrue(plan.has_integrity_failures)

    def test_repeatable_checksum_drift_is_pending_not_integrity_failure(self):
        self._migration("0001", "repeatable")
        migs = self._discover()
        applied = {"0001": self._applied(migs[0], checksum="deadbeef" * 8)}
        plan = _plan_status(migs, applied)
        self.assertEqual(plan.one_time_mismatches, ())
        self.assertEqual(plan.mode_mismatches, ())
        self.assertEqual([m.version for m in plan.pending_repeatable_changed], ["0001"])
        self.assertFalse(plan.has_integrity_failures)

    def test_execution_mode_mismatch_detected_even_with_matching_checksum(self):
        self._migration("0001", "one_time")
        migs = self._discover()
        applied = {"0001": self._applied(migs[0], execution=ExecutionMode.REPEATABLE)}
        plan = _plan_status(migs, applied)
        self.assertEqual(plan.mode_mismatches, ("0001",))
        self.assertEqual(plan.one_time_mismatches, ())
        self.assertEqual(plan.pending, ())
        self.assertTrue(plan.has_integrity_failures)

    def test_execution_mode_mismatch_takes_priority_over_checksum_check(self):
        # Both mode AND checksum differ: this must surface as a mode
        # mismatch, not also (or instead) as a one_time checksum mismatch.
        self._migration("0001", "one_time")
        migs = self._discover()
        applied = {
            "0001": self._applied(migs[0], checksum="deadbeef" * 8, execution=ExecutionMode.REPEATABLE)
        }
        plan = _plan_status(migs, applied)
        self.assertEqual(plan.mode_mismatches, ("0001",))
        self.assertEqual(plan.one_time_mismatches, ())

    def test_pending_property_orders_one_time_before_repeatable_initial_before_repeatable_changed(self):
        self._migration("0001", "repeatable")  # will be "changed"
        self._migration("0002", "one_time")  # will be "pending one-time"
        self._migration("0003", "repeatable")  # will be "pending initial"
        migs = self._discover()
        by_version = {m.version: m for m in migs}
        applied = {"0001": self._applied(by_version["0001"], checksum="deadbeef" * 8)}
        plan = _plan_status(migs, applied)
        self.assertEqual(
            [m.version for m in plan.pending],
            ["0002", "0003", "0001"],
        )

    def test_has_integrity_failures_false_for_all_pending_states_combined(self):
        self._migration("0001", "one_time")  # pending
        self._migration("0002", "repeatable")  # pending initial
        migs = self._discover()
        plan = _plan_status(migs, {})
        self.assertFalse(plan.has_integrity_failures)

    def test_has_integrity_failures_true_for_mode_mismatch_alone(self):
        self._migration("0001", "one_time")
        migs = self._discover()
        applied = {"0001": self._applied(migs[0], execution=ExecutionMode.REPEATABLE)}
        plan = _plan_status(migs, applied)
        self.assertTrue(plan.has_integrity_failures)

    def test_mixed_versions_classified_independently(self):
        self._migration("0001", "one_time")  # applied, current
        self._migration("0002", "repeatable")  # applied, current
        self._migration("0003", "one_time")  # pending
        self._migration("0004", "repeatable")  # pending initial
        migs = self._discover()
        by_version = {m.version: m for m in migs}
        applied = {
            "0001": self._applied(by_version["0001"]),
            "0002": self._applied(by_version["0002"]),
        }
        plan = _plan_status(migs, applied)
        self.assertEqual([m.version for m in plan.applied_one_time], ["0001"])
        self.assertEqual([m.version for m in plan.applied_repeatable_current], ["0002"])
        self.assertEqual([m.version for m in plan.pending_one_time], ["0003"])
        self.assertEqual([m.version for m in plan.pending_repeatable_initial], ["0004"])
        self.assertFalse(plan.has_integrity_failures)


class _FakeCursor:
    """Minimal stand-in for a psycopg cursor, scripted by _FakeHistoryConn
    below purely by inspecting the SQL text -- just enough to exercise
    _history_has_execution_columns()/_read_applied()'s query-shape
    branching without a real database."""

    def __init__(self, conn):
        self._conn = conn
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_lower = " ".join(sql.lower().split())
        if "information_schema.columns" in sql_lower and "column_name = 'execution'" in sql_lower:
            self._result = [(1,)] if self._conn.has_execution_columns else []
        elif "execution, execution_count" in sql_lower and "schema_migrations" in sql_lower:
            self._result = self._conn.new_shape_rows
        elif "schema_migrations" in sql_lower and "select version, description, checksum" in sql_lower:
            self._result = self._conn.old_shape_rows
        else:
            raise AssertionError(f"_FakeCursor received an unexpected query: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeHistoryConn:
    def __init__(self, *, has_execution_columns: bool, rows: list[tuple]):
        self.has_execution_columns = has_execution_columns
        self.new_shape_rows = rows if has_execution_columns else []
        self.old_shape_rows = rows if not has_execution_columns else []

    def cursor(self):
        return _FakeCursor(self)


class TestHistoryTableCompatibility(unittest.TestCase):
    """_history_has_execution_columns()/_read_applied() must correctly
    detect and read both the pre-upgrade (six-column) and post-upgrade
    (eight-column) schema_migrations shapes -- exercised here against a
    fake cursor so the branching logic itself is unit-tested without a
    real database (see the integration suite for the real upgrade DDL)."""

    def test_detects_old_shape_missing_execution_columns(self):
        conn = _FakeHistoryConn(has_execution_columns=False, rows=[])
        self.assertFalse(_history_has_execution_columns(conn, "tuva_ops"))

    def test_detects_new_shape_with_execution_columns(self):
        conn = _FakeHistoryConn(has_execution_columns=True, rows=[])
        self.assertTrue(_history_has_execution_columns(conn, "tuva_ops"))

    def test_read_applied_old_shape_defaults_to_one_time_execution_count_one(self):
        applied_at = datetime.now(timezone.utc)
        conn = _FakeHistoryConn(
            has_execution_columns=False,
            rows=[("0001", "legacy migration", "abc123", applied_at, 12.5, "0.0.0")],
        )
        applied = _read_applied(conn, "tuva_ops")
        self.assertEqual(set(applied), {"0001"})
        record = applied["0001"]
        self.assertEqual(record.checksum, "abc123")
        self.assertEqual(record.execution, ExecutionMode.ONE_TIME)
        self.assertEqual(record.execution_count, 1)
        # Nothing about the pre-existing columns is altered in translation.
        self.assertEqual(record.description, "legacy migration")
        self.assertEqual(record.duration_ms, 12.5)
        self.assertEqual(record.app_version, "0.0.0")

    def test_read_applied_new_shape_preserves_execution_and_count(self):
        applied_at = datetime.now(timezone.utc)
        conn = _FakeHistoryConn(
            has_execution_columns=True,
            rows=[
                ("0001", "baseline", "abc123", applied_at, 12.5, "0.1.0", "one_time", 1),
                ("0002", "a view", "def456", applied_at, 3.0, "0.1.0", "repeatable", 4),
            ],
        )
        applied = _read_applied(conn, "tuva_ops")
        self.assertEqual(applied["0001"].execution, ExecutionMode.ONE_TIME)
        self.assertEqual(applied["0001"].execution_count, 1)
        self.assertEqual(applied["0002"].execution, ExecutionMode.REPEATABLE)
        self.assertEqual(applied["0002"].execution_count, 4)

    def test_read_applied_empty_history_returns_empty_mapping(self):
        conn = _FakeHistoryConn(has_execution_columns=True, rows=[])
        self.assertEqual(_read_applied(conn, "tuva_ops"), {})


if __name__ == "__main__":
    unittest.main()
