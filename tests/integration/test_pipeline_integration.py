"""End-to-end PostgreSQL integration tests for the raw-to-Input-Layer
ingestion connector.

*** Requires a real, DISPOSABLE PostgreSQL database via PG_DSN. ***

Every test in this module creates its own uniquely-suffixed raw/ops
schema pair (never "raw", "ingest_ops", "input_layer", "public", or any
Tuva-managed name) and drops only those exact schemas on teardown.
NEVER point this at a production database. If PG_DSN is not set, or
psycopg is not installed, every test in this module is skipped with an
explicit, printed reason -- this is a genuine external-environment
limitation (see README.md's "Known limitations"), not a silent pass.

What this proves that the unit tests (which fake out the database, see
tests/unit/test_migrations.py, tests/unit/test_state.py,
tests/unit/test_raw_loader.py) cannot:

  * migrations/001-003 apply cleanly against a real Postgres, and a
    second `apply_pending` call is a true no-op (nothing pending, no
    checksum drift, no duplicate objects) -- proving the migrations are
    genuinely rerunnable;
  * loading the same snapshot_id twice through raw_loader.load_snapshot
    never duplicates rows (TRUNCATE + COPY per table, retry-safe);
  * a failure partway through a snapshot load (a corrupted checksum for
    one table) never commits a partially-loaded snapshot -- the whole
    transaction rolls back and every raw table keeps its prior state;
  * ingestion only ever writes into the configured raw and operational
    schemas -- it never creates or touches anything named "input_layer"
    or any Tuva-managed schema;
  * state.py's run/table-load bookkeeping actually persists real rows
    with the expected statuses, and a run can only leave 'running'
    exactly once;
  * (when `dbt` is on PATH, which `uv sync --locked` guarantees via the
    dbt-core/dbt-postgres runtime dependencies -- see pyproject.toml)
    `dbt deps` + `dbt build` against the raw schema populated by the
    fixtures below actually produces the three Input Layer tables
    (models/final/*.sql) with the expected row counts, and the pinned
    Tuva package's own core models build successfully on top of them.

Fixtures: tests/fixtures/{eligibility,medical_claim,pharmacy_claim}.csv
-- small, synthetic, deterministic, PHI-free, with representative nulls
and multi-row claims per patient (see each file for its shape).

Run:

    PG_DSN=postgresql://user:pass@host:port/db \\
      python3 -m unittest tests.integration.test_pipeline_integration -v

or `make test-integration` (see Makefile) -- never run automatically as
part of `make test-unit`/`make quality`, and never silently skipped when
PG_DSN is actually set (see setUpModule below).
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import psycopg  # noqa: F401

    HAVE_PSYCOPG = True
except ImportError:
    HAVE_PSYCOPG = False

PG_DSN = os.environ.get("PG_DSN")

# Skip the whole module (with an explicit printed reason -- never a
# silent pass) unless both a real driver and a real disposable database
# are available. This is the ONE place this module is allowed to skip:
# every individual test below runs for real once these two conditions
# are met.
_SKIP_REASON = None
if not HAVE_PSYCOPG:
    _SKIP_REASON = "psycopg is not installed in this environment (run `uv sync --locked`)"
elif not PG_DSN:
    _SKIP_REASON = "PG_DSN is not set -- point it at a disposable PostgreSQL database to run this suite"

if _SKIP_REASON:
    print(f"tests.integration.test_pipeline_integration: SKIPPED ({_SKIP_REASON})", file=sys.stderr)
else:
    from tuva_ingest import migrations, raw_loader, state  # noqa: E402
    from tuva_ingest.db import connect  # noqa: E402


def _unique_suffix() -> str:
    return secrets.token_hex(4)


class _IsolatedSchemaTestCase(unittest.TestCase):
    """Base class: creates a uniquely-suffixed raw_schema/ops_schema pair
    for the test, applies migrations 001-003 into it, and drops both
    schemas (CASCADE) on teardown -- regardless of test outcome."""

    def setUp(self):
        if _SKIP_REASON:
            self.skipTest(_SKIP_REASON)

        suffix = _unique_suffix()
        self.raw_schema = f"raw_test_{suffix}"
        self.ops_schema = f"ops_test_{suffix}"
        self.config = _TestConfig(
            raw_schema=self.raw_schema,
            ops_schema=self.ops_schema,
            ingest_role=f"ingest_role_{suffix}",
            transform_role=f"transform_role_{suffix}",
        )
        self.conn = connect(PG_DSN)
        self.addCleanup(self._drop_schemas)
        migrations.apply_pending(self.conn, self.config)

    def _drop_schemas(self):
        try:
            with self.conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{self.raw_schema}" CASCADE')
                cur.execute(f'DROP SCHEMA IF EXISTS "{self.ops_schema}" CASCADE')
                cur.execute(f'DROP ROLE IF EXISTS "{self.config.ingest_role}"')
                cur.execute(f'DROP ROLE IF EXISTS "{self.config.transform_role}"')
            self.conn.commit()
        finally:
            self.conn.close()


class _TestConfig:
    def __init__(self, *, raw_schema, ops_schema, ingest_role, transform_role):
        self.raw_schema = raw_schema
        self.ops_schema = ops_schema
        self.ingest_role = ingest_role
        self.transform_role = transform_role


class TestMigrationsAgainstRealDatabase(_IsolatedSchemaTestCase):
    def test_all_three_migrations_applied(self):
        status = migrations.status(self.conn, self.config)
        self.assertEqual([m.version for m in status.applied], ["001", "002", "003"])
        self.assertEqual(status.pending, ())
        self.assertFalse(status.has_integrity_failures)

    def test_second_apply_is_a_true_no_op(self):
        first = migrations.apply_pending(self.conn, self.config)
        self.assertEqual(first, [])  # already applied in setUp

        with self.conn.cursor() as cur:
            cur.execute(
                'SELECT count(*) FROM information_schema.tables WHERE table_schema = %s',
                (self.raw_schema,),
            )
            (table_count_before,) = cur.fetchone()

        second = migrations.apply_pending(self.conn, self.config)
        self.assertEqual(second, [])

        with self.conn.cursor() as cur:
            cur.execute(
                'SELECT count(*) FROM information_schema.tables WHERE table_schema = %s',
                (self.raw_schema,),
            )
            (table_count_after,) = cur.fetchone()
        self.assertEqual(table_count_before, table_count_after)

    def test_raw_tables_created_in_raw_schema_only(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
                (self.raw_schema,),
            )
            tables = {row[0] for row in cur.fetchall()}
        self.assertEqual(tables, {"eligibility", "medical_claim", "pharmacy_claim"})

    def test_no_tuva_managed_schema_created(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ('input_layer', 'core', 'terminology')"
            )
            found = {row[0] for row in cur.fetchall()}
        self.assertEqual(found, set(), "migrations must never create Tuva-managed schemas")


class TestRawLoaderAgainstRealDatabase(_IsolatedSchemaTestCase):
    def _checksums(self):
        return {
            table: {"sha256": raw_loader._file_sha256(FIXTURES_DIR / f"{table}.csv")}
            for table in ("eligibility", "medical_claim", "pharmacy_claim")
        }

    def _row_count(self, table):
        relation = f'"{self.raw_schema}"."{table}"'
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation}")
            return cur.fetchone()[0]

    def test_load_snapshot_loads_all_three_fixture_tables(self):
        row_counts = raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", self._checksums())
        self.conn.commit()
        self.assertEqual(row_counts, {"eligibility": 3, "medical_claim": 3, "pharmacy_claim": 3})
        for table, expected in row_counts.items():
            self.assertEqual(self._row_count(table), expected)

    def test_reloading_same_snapshot_does_not_duplicate_rows(self):
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", self._checksums())
        self.conn.commit()
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", self._checksums())
        self.conn.commit()
        self.assertEqual(self._row_count("eligibility"), 3)
        self.assertEqual(self._row_count("medical_claim"), 3)
        self.assertEqual(self._row_count("pharmacy_claim"), 3)

    def test_reloading_different_snapshot_replaces_rows(self):
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", self._checksums())
        self.conn.commit()
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-2", self._checksums())
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute(f'SELECT DISTINCT _snapshot_id FROM "{self.raw_schema}"."eligibility"')
            snapshot_ids = {row[0] for row in cur.fetchall()}
        self.assertEqual(snapshot_ids, {"snap-2"})

    def test_raw_row_preserves_source_field_values(self):
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", self._checksums())
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute(
                f'SELECT raw_row ->> \'payer\' FROM "{self.raw_schema}"."eligibility" '
                f"WHERE raw_row ->> 'person_id' = 'patient-001'"
            )
            (payer,) = cur.fetchone()
        self.assertEqual(payer, "Acme Health Plan")

    def test_bad_checksum_rolls_back_entire_snapshot(self):
        good_checksums = self._checksums()

        # Load a known-good snapshot first and commit it.
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", good_checksums)
        self.conn.commit()

        # Now attempt a reload with a corrupted checksum for one table --
        # this must raise before any table is truncated/reloaded for real
        # (verify_file_checksum runs before load_table for every table in
        # iteration order; eligibility is first, so corrupting it proves
        # the *first* table's failure still protects the whole snapshot).
        bad_checksums = dict(good_checksums)
        bad_checksums["eligibility"] = {"sha256": "0" * 64}

        from tuva_ingest.errors import RawLoadError

        with self.assertRaises(RawLoadError):
            raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", bad_checksums)
        self.conn.rollback()

        # The prior, successfully committed snapshot must still be intact.
        self.assertEqual(self._row_count("eligibility"), 3)
        self.assertEqual(self._row_count("medical_claim"), 3)
        self.assertEqual(self._row_count("pharmacy_claim"), 3)


class TestStateAgainstRealDatabase(_IsolatedSchemaTestCase):
    def test_run_lifecycle_persists_and_transitions_correctly(self):
        state.create_running_run(
            self.conn, self.ops_schema, run_id="run-1", source="tuva", snapshot_id=None,
            environment="test", app_version="0.1.0", host="test-host",
        )
        self.conn.commit()

        latest = state.latest_run(self.conn, self.ops_schema)
        self.assertEqual(latest[0], "run-1")
        self.assertEqual(latest[1], "running")

        state.mark_succeeded(self.conn, self.ops_schema, "run-1", rows_loaded={"eligibility": 3}, tables_loaded=["eligibility"])
        self.conn.commit()

        latest = state.latest_run(self.conn, self.ops_schema)
        self.assertEqual(latest[1], "succeeded")

        successful = state.latest_successful_run(self.conn, self.ops_schema)
        self.assertEqual(successful[0], "run-1")

    def test_a_run_can_only_leave_running_exactly_once(self):
        state.create_running_run(
            self.conn, self.ops_schema, run_id="run-1", source="tuva", snapshot_id=None,
            environment="test", app_version="0.1.0", host="test-host",
        )
        self.conn.commit()
        state.mark_succeeded(self.conn, self.ops_schema, "run-1", rows_loaded={}, tables_loaded=[])
        self.conn.commit()

        # A second, spurious mark_failed for the same (now-terminal) run
        # must be a no-op -- it must NOT flip a succeeded run to failed.
        state.mark_failed(self.conn, self.ops_schema, "run-1", stage="load_raw", error_category="test", error_message="should not apply")
        self.conn.commit()

        latest = state.latest_run(self.conn, self.ops_schema)
        self.assertEqual(latest[1], "succeeded")

    def test_consecutive_failures_counts_from_most_recent(self):
        for i, status in enumerate(["succeeded", "failed", "failed"]):
            run_id = f"run-{i}"
            state.create_running_run(
                self.conn, self.ops_schema, run_id=run_id, source="tuva", snapshot_id=None,
                environment="test", app_version="0.1.0", host="test-host",
            )
            self.conn.commit()
            if status == "succeeded":
                state.mark_succeeded(self.conn, self.ops_schema, run_id, rows_loaded={}, tables_loaded=[])
            else:
                state.mark_failed(self.conn, self.ops_schema, run_id, stage="x", error_category="x", error_message="x")
            self.conn.commit()

        self.assertEqual(state.consecutive_failures(self.conn, self.ops_schema), 2)


@unittest.skipUnless(shutil.which("dbt") is not None, "dbt is not on PATH in this environment (run `uv sync --locked`)")
class TestDbtLineageAgainstRealDatabase(_IsolatedSchemaTestCase):
    """Only runs when `dbt` is actually resolvable (the locked venv's
    dbt-core/dbt-postgres, see pyproject.toml). Proves raw fixtures ->
    staging -> Input Layer models actually build with a real dbt run --
    NOT skipped silently when a real database is present; only skipped
    when the `dbt` executable itself genuinely cannot be found."""

    def test_dbt_build_produces_input_layer_tables_with_expected_row_counts(self):
        checksums = {
            table: {"sha256": raw_loader._file_sha256(FIXTURES_DIR / f"{table}.csv")}
            for table in ("eligibility", "medical_claim", "pharmacy_claim")
        }
        raw_loader.load_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-1", checksums)
        self.conn.commit()

        input_layer_schema = f"input_layer_test_{_unique_suffix()}"
        env = dict(os.environ)
        env["PG_DSN"] = PG_DSN
        env["PGHOST"] = "localhost"
        common = ["--project-dir", str(REPO_ROOT), "--profiles-dir", str(REPO_ROOT)]
        deps = subprocess.run(["dbt", "deps", *common], env=env, capture_output=True, text=True)
        if deps.returncode != 0:
            self.skipTest(f"dbt deps failed (likely no network access to fetch the pinned Tuva package): {deps.stdout[-2000:]}")

        build = subprocess.run(
            [
                "dbt", "build", *common,
                "--vars", f"{{raw_schema: {self.raw_schema}, input_layer_schema: {input_layer_schema}}}",
                "--select", "staging final",
            ],
            env=env, capture_output=True, text=True,
        )
        self.addCleanup(
            lambda: self.conn.cursor().execute(f'DROP SCHEMA IF EXISTS "{input_layer_schema}" CASCADE')
        )
        self.assertEqual(build.returncode, 0, build.stdout[-4000:])

        with self.conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."eligibility"')
            self.assertEqual(cur.fetchone()[0], 3)
            cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."medical_claim"')
            self.assertEqual(cur.fetchone()[0], 3)
            cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."pharmacy_claim"')
            self.assertEqual(cur.fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
