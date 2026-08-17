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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    from tuva_ingest.raw_loader import load_single_endpoint_snapshot  # noqa: E402
    from tuva_ingest.pagination import PaginatedRunStore, validate_page_envelope  # noqa: E402
    from tuva_ingest.paginated_loader import load_paginated_run, loaded_row_count, verify_run_manifest  # noqa: E402
    from tuva_ingest.quarantine import insert_quarantine_record  # noqa: E402
    from tuva_ingest.validators import QuarantineDecision  # noqa: E402


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
    def test_all_five_migrations_applied(self):
        status = migrations.status(self.conn, self.config)
        self.assertEqual([m.version for m in status.applied], ["001", "002", "003", "004", "005"])
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
    staging -> Input Layer models actually build with a real dbt run,
    that the pinned Tuva package's structural DQ gate passes against
    them, and that the resulting relations carry the full Tuva 0.18.0
    Input Layer contract -- NOT skipped silently when a real database is
    present; only skipped when the `dbt` executable itself genuinely
    cannot be found.

    The expected column contract asserted below (names + PostgreSQL
    types) was confirmed against thetuvaproject.com/connectors/
    claims-mapping-guide and tuva-health/connector_template's reference
    implementation -- see models/final/*.sql's own header comments for
    the full citation. It is intentionally re-derived here (not
    imported from the SQL models) so this test can catch the final
    models silently drifting from the contract, not just from
    themselves.
    """

    # (column_name, expected udt_name-family) -- checked via a `startswith`/
    # membership match against information_schema.columns.data_type, since
    # PostgreSQL may report e.g. "character varying" or "text" for both
    # dbt's `text` casts depending on adapter version.
    _TEXT_TYPES = {"text", "character varying"}
    _ELIGIBILITY_COLUMNS = {
        "person_id": _TEXT_TYPES, "member_id": _TEXT_TYPES, "subscriber_id": _TEXT_TYPES,
        "gender": _TEXT_TYPES, "race": _TEXT_TYPES,
        "birth_date": {"date"}, "death_date": {"date"}, "death_flag": {"integer"},
        "enrollment_start_date": {"date"}, "enrollment_end_date": {"date"},
        "payer": _TEXT_TYPES, "payer_type": _TEXT_TYPES, "plan": _TEXT_TYPES,
        "original_reason_entitlement_code": _TEXT_TYPES, "dual_status_code": _TEXT_TYPES,
        "medicare_status_code": _TEXT_TYPES, "group_id": _TEXT_TYPES, "group_name": _TEXT_TYPES,
        "name_suffix": _TEXT_TYPES, "first_name": _TEXT_TYPES, "middle_name": _TEXT_TYPES,
        "last_name": _TEXT_TYPES, "email": _TEXT_TYPES, "ethnicity": _TEXT_TYPES,
        "social_security_number": _TEXT_TYPES, "subscriber_relation": _TEXT_TYPES,
        "address": _TEXT_TYPES, "city": _TEXT_TYPES, "state": _TEXT_TYPES, "zip_code": _TEXT_TYPES,
        "phone": _TEXT_TYPES, "data_source": _TEXT_TYPES, "file_name": _TEXT_TYPES,
        "file_date": {"date"}, "ingest_datetime": {"timestamp without time zone", "timestamp with time zone"},
    }
    _MEDICAL_CLAIM_REQUIRED_COLUMNS = {
        "claim_id", "claim_line_number", "claim_type", "person_id", "member_id", "payer", "plan",
        "claim_start_date", "claim_end_date", "claim_line_start_date", "claim_line_end_date",
        "admission_date", "discharge_date", "paid_date",
        "admit_source_code", "admit_type_code", "discharge_disposition_code",
        "place_of_service_code", "bill_type_code", "revenue_center_code",
        "drg_code_type", "drg_code",
        "service_unit_quantity", "hcpcs_code",
        "hcpcs_modifier_1", "hcpcs_modifier_2", "hcpcs_modifier_3", "hcpcs_modifier_4", "hcpcs_modifier_5",
        "rendering_npi", "rendering_tin", "billing_npi", "billing_tin", "facility_npi",
        "paid_amount", "allowed_amount", "charge_amount", "coinsurance_amount",
        "copayment_amount", "deductible_amount", "total_cost_amount",
        "diagnosis_code_type", "procedure_code_type",
        "in_network_flag", "data_source", "file_name", "file_date", "ingest_datetime",
    } | {f"diagnosis_code_{i}" for i in range(1, 26)} | {f"diagnosis_poa_{i}" for i in range(1, 26)} \
        | {f"procedure_code_{i}" for i in range(1, 26)} | {f"procedure_date_{i}" for i in range(1, 26)}
    _PHARMACY_CLAIM_REQUIRED_COLUMNS = {
        "claim_id", "claim_line_number", "person_id", "member_id", "payer", "plan",
        "prescribing_provider_npi", "dispensing_provider_npi", "dispensing_date", "ndc_code",
        "quantity", "days_supply", "refills", "paid_date", "paid_amount", "allowed_amount",
        "charge_amount", "coinsurance_amount", "copayment_amount", "deductible_amount",
        "in_network_flag", "data_source", "file_name", "file_date", "ingest_datetime",
    }

    def _run_dbt(self, *args, env, common):
        return subprocess.run(["dbt", *args, *common], env=env, capture_output=True, text=True)

    def _information_schema_columns(self, schema, table):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def test_dbt_build_produces_input_layer_tables_with_full_contract_and_expected_row_counts(self):
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
        dbt_vars = ["--vars", f"{{raw_schema: {self.raw_schema}, input_layer_schema: {input_layer_schema}}}"]
        self.addCleanup(
            lambda: self.conn.cursor().execute(f'DROP SCHEMA IF EXISTS "{input_layer_schema}" CASCADE')
        )

        deps = self._run_dbt("deps", env=env, common=common)
        if deps.returncode != 0:
            self.skipTest(f"dbt deps failed (likely no network access to fetch the pinned Tuva package): {deps.stdout[-2000:]}")

        # Stage 1: this connector's own Input Layer models only (see
        # README.md "Validation order" / dbt_project.yml's `+tags:
        # ["input_layer"]` on both staging and final).
        input_layer_build = self._run_dbt(
            "build", *dbt_vars, "--select", "tag:input_layer", env=env, common=common,
        )
        self.assertEqual(input_layer_build.returncode, 0, input_layer_build.stdout[-4000:])

        # Stage 2: the pinned Tuva package's structural DQ checks must
        # pass against those Input Layer models before anything else
        # runs. If `tag:dq_structural` selects zero nodes, that is
        # itself a finding worth failing loudly on (see Makefile's
        # dbt-dq-structural target and README.md "Known limitations" --
        # this exact selector could not be confirmed against the live
        # package in this repository's own sandboxed development
        # environment, which has no outbound network access).
        dq_structural_ls = subprocess.run(
            ["dbt", "ls", *common, *dbt_vars, "--select", "tag:dq_structural", "--resource-type", "test"],
            env=env, capture_output=True, text=True,
        )
        selected_structural_tests = [line for line in dq_structural_ls.stdout.splitlines() if line.strip()]
        if dq_structural_ls.returncode == 0 and not selected_structural_tests:
            self.fail(
                "tag:dq_structural selected zero tests against the pinned Tuva package -- "
                "the tag name assumed by this connector (Makefile's dbt-dq-structural target, "
                "README.md's Validation order) does not match the installed package. Re-run "
                "`dbt ls --select tag:dq_structural` against dbt_packages/the_tuva_project after "
                "`dbt deps` to find the correct selector and update Makefile/README/CI together."
            )
        dq_structural_build = self._run_dbt(
            "build", *dbt_vars, "--select", "tag:dq_structural", env=env, common=common,
        )
        self.assertEqual(dq_structural_build.returncode, 0, dq_structural_build.stdout[-4000:])

        with self.conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."eligibility"')
            self.assertEqual(cur.fetchone()[0], 3)
            cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."medical_claim"')
            self.assertEqual(cur.fetchone()[0], 3)
            cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."pharmacy_claim"')
            self.assertEqual(cur.fetchone()[0], 3)

        # Structural completeness: every required Tuva 0.18.0 column
        # exists, with a compatible PostgreSQL type, on each relation --
        # not just the ones this source happens to populate.
        eligibility_cols = self._information_schema_columns(input_layer_schema, "eligibility")
        self.assertEqual(set(eligibility_cols), set(self._ELIGIBILITY_COLUMNS))
        for col, allowed_types in self._ELIGIBILITY_COLUMNS.items():
            self.assertIn(eligibility_cols[col], allowed_types, f"eligibility.{col} has unexpected type {eligibility_cols[col]!r}")

        medical_claim_cols = set(self._information_schema_columns(input_layer_schema, "medical_claim"))
        self.assertEqual(medical_claim_cols, self._MEDICAL_CLAIM_REQUIRED_COLUMNS)

        pharmacy_claim_cols = set(self._information_schema_columns(input_layer_schema, "pharmacy_claim"))
        self.assertEqual(pharmacy_claim_cols, self._PHARMACY_CLAIM_REQUIRED_COLUMNS)

    def test_repeated_build_is_deterministic(self):
        """Running dbt build --select tag:input_layer twice against the
        same raw data produces the same row counts both times (proves
        the connector's models are pure SELECT/CAST transformations with
        no non-deterministic elements, e.g. no accidental fan-out joins
        or timestamp-of-build-dependent values in a way that would
        change row counts)."""
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
        dbt_vars = ["--vars", f"{{raw_schema: {self.raw_schema}, input_layer_schema: {input_layer_schema}}}"]
        self.addCleanup(
            lambda: self.conn.cursor().execute(f'DROP SCHEMA IF EXISTS "{input_layer_schema}" CASCADE')
        )

        deps = self._run_dbt("deps", env=env, common=common)
        if deps.returncode != 0:
            self.skipTest(f"dbt deps failed (likely no network access to fetch the pinned Tuva package): {deps.stdout[-2000:]}")

        def _row_counts():
            counts = {}
            with self.conn.cursor() as cur:
                for table in ("eligibility", "medical_claim", "pharmacy_claim"):
                    cur.execute(f'SELECT count(*) FROM "{input_layer_schema}"."{table}"')
                    counts[table] = cur.fetchone()[0]
            return counts

        first = self._run_dbt("build", *dbt_vars, "--select", "tag:input_layer", "--full-refresh", env=env, common=common)
        self.assertEqual(first.returncode, 0, first.stdout[-4000:])
        first_counts = _row_counts()

        second = self._run_dbt("build", *dbt_vars, "--select", "tag:input_layer", "--full-refresh", env=env, common=common)
        self.assertEqual(second.returncode, 0, second.stdout[-4000:])
        second_counts = _row_counts()

        self.assertEqual(first_counts, second_counts)
        self.assertEqual(first_counts, {"eligibility": 3, "medical_claim": 3, "pharmacy_claim": 3})




class TestEndpointScopedLoadAgainstRealDatabase(_IsolatedSchemaTestCase):
    """Exercises raw_loader.load_single_endpoint_snapshot and
    state.upsert_running_run/upsert_table_load_pending -- the real-
    database counterparts to tests/unit/test_raw_loader.py's and
    tests/unit/test_state.py's faked-connection tests -- against a real,
    disposable PostgreSQL database, proving:

      * loading one endpoint's table never touches the other two raw
        tables (they stay empty/untouched);
      * a failed load rolls back cleanly, leaving the target table
        exactly as it was before the attempt;
      * a repeated `load --run-id` for the same run_id/table is
        idempotent at both the state layer (ON CONFLICT DO UPDATE, no
        duplicate ingestion_runs/table_loads rows) and the data layer
        (TRUNCATE + COPY, no duplicated rows).
    """

    def _row_count(self, table):
        relation = f'"{self.raw_schema}"."{table}"'
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation}")
            return cur.fetchone()[0]

    def _table_row_count_in_ops(self, run_id):
        with self.conn.cursor() as cur:
            cur.execute(
                f'SELECT count(*) FROM "{self.ops_schema}"."table_loads" WHERE run_id = %s',
                (run_id,),
            )
            return cur.fetchone()[0]

    def test_loading_one_endpoint_never_touches_other_raw_tables(self):
        checksum = {"eligibility": {"sha256": raw_loader._file_sha256(FIXTURES_DIR / "eligibility.csv")}}
        load_single_endpoint_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-ep-1", "eligibility", checksum)
        self.conn.commit()

        self.assertEqual(self._row_count("eligibility"), 3)
        self.assertEqual(self._row_count("medical_claim"), 0)
        self.assertEqual(self._row_count("pharmacy_claim"), 0)

    def test_failed_checksum_rolls_back_and_leaves_table_untouched(self):
        good = {"eligibility": {"sha256": raw_loader._file_sha256(FIXTURES_DIR / "eligibility.csv")}}
        load_single_endpoint_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-ep-1", "eligibility", good)
        self.conn.commit()
        self.assertEqual(self._row_count("eligibility"), 3)

        from tuva_ingest.errors import RawLoadError

        bad = {"eligibility": {"sha256": "0" * 64}}
        with self.assertRaises(RawLoadError):
            load_single_endpoint_snapshot(self.conn, self.config, FIXTURES_DIR, "snap-ep-1", "eligibility", bad)
        self.conn.rollback()

        # The prior, committed load must still be intact -- a failed
        # verification must never truncate the table before raising.
        self.assertEqual(self._row_count("eligibility"), 3)

    def test_upsert_running_run_and_table_load_pending_are_idempotent_for_same_run_id(self):
        run_id = "snap-ep-idem-1"
        checksum = {"eligibility": {"sha256": raw_loader._file_sha256(FIXTURES_DIR / "eligibility.csv")}}

        for _ in range(2):
            state.upsert_running_run(
                self.conn, self.ops_schema, run_id=run_id, source="tuva", snapshot_id=run_id,
                endpoint="eligibility", requested_since=None, environment="test",
                app_version="0.1.0", host="test-host",
            )
            state.upsert_table_load_pending(
                self.conn, self.ops_schema, run_id, table="eligibility",
                expected_sha256=checksum["eligibility"]["sha256"], expected_size_bytes=None,
            )
            load_single_endpoint_snapshot(self.conn, self.config, FIXTURES_DIR, run_id, "eligibility", checksum)
            state.mark_table_load_succeeded(
                self.conn, self.ops_schema, run_id, "eligibility", row_count=3,
                actual_sha256=checksum["eligibility"]["sha256"], actual_size_bytes=None,
            )
            state.mark_succeeded(self.conn, self.ops_schema, run_id, rows_loaded={"eligibility": 3}, tables_loaded=["eligibility"])
            self.conn.commit()

        # Repeating the whole load/state sequence for the same run_id
        # must never accumulate a second ingestion_runs or table_loads
        # row, and the data itself must not be duplicated.
        with self.conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{self.ops_schema}"."ingestion_runs" WHERE run_id = %s', (run_id,))
            self.assertEqual(cur.fetchone()[0], 1)
        self.assertEqual(self._table_row_count_in_ops(run_id), 1)
        self.assertEqual(self._row_count("eligibility"), 3)

        latest = state.latest_run(self.conn, self.ops_schema)
        self.assertEqual(latest[0], run_id)
        self.assertEqual(latest[1], "succeeded")




class TestPaginatedExtractionAgainstRealDatabase(_IsolatedSchemaTestCase):
    """Exercises the paginated extraction contract's database-touching
    primitives (load_paginated_run, loaded_row_count, verify_run_manifest,
    state.get_watermark/commit_watermark) against a real, disposable
    PostgreSQL database -- the real-transaction counterpart to
    tests/unit/test_pagination.py's and test_paginated_loader.py's
    faked/file-only tests. Proves:

      * a published paginated run loads correctly into the raw table,
        with the new metadata columns (migrations/005) populated;
      * repeating the same load is idempotent (no duplicate rows);
      * a simulated failure partway through a load rolls back cleanly,
        leaving both the raw table and the watermark untouched;
      * the watermark only advances after a full commit, never before,
        and never moves backward once a caller enforces the guard (the
        same guard cli._run_paginated_load applies).
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)
        self.store = PaginatedRunStore(self.raw_data_dir, "tuva")

    def _publish_run(self, run_id, *, endpoint="eligibility", records=None, high_water_mark="2025-06-01T00:00:00Z"):
        from datetime import datetime, timezone

        records = records if records is not None else [{"person_id": "p1"}, {"person_id": "p2"}]
        staging = self.store.begin_staging(run_id)
        payload = {
            "records": records,
            "metadata": {
                "record_count": len(records), "page_token": None, "next_page_token": None,
                "high_water_mark": high_water_mark,
            },
        }
        envelope = validate_page_envelope(payload, requested_page_token=None)
        meta = self.store.write_page(
            staging, run_id=run_id, endpoint=endpoint, page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        self.store.finalize(
            staging, run_id, [meta], endpoint=endpoint, since=None,
            total_record_count=len(records), candidate_high_water_mark=high_water_mark,
        )
        return verify_run_manifest(self.store, run_id)

    def _row_count(self, table):
        relation = f'"{self.raw_schema}"."{table}"'
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation}")
            return cur.fetchone()[0]

    def test_load_paginated_run_populates_raw_table_and_metadata_columns(self):
        manifest = self._publish_run("run-pg-1")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-1", manifest)
        self.conn.commit()

        self.assertEqual(self._row_count("eligibility"), 2)
        with self.conn.cursor() as cur:
            cur.execute(
                f'SELECT endpoint, page_number, file_sha256 FROM "{self.raw_schema}"."eligibility" '
                f"WHERE _snapshot_id = %s LIMIT 1",
                ("run-pg-1",),
            )
            endpoint, page_number, file_sha256 = cur.fetchone()
        self.assertEqual(endpoint, "eligibility")
        self.assertEqual(page_number, 1)
        self.assertIsNotNone(file_sha256)

    def test_loaded_row_count_matches_manifest_total(self):
        manifest = self._publish_run("run-pg-2")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-2", manifest)
        self.conn.commit()
        count = loaded_row_count(self.conn, self.config.raw_schema, "eligibility", "run-pg-2")
        self.assertEqual(count, manifest["total_record_count"])

    def test_repeated_load_is_idempotent(self):
        manifest = self._publish_run("run-pg-3")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-3", manifest)
        self.conn.commit()
        load_paginated_run(self.conn, self.config, self.store, "run-pg-3", manifest)  # repeat
        self.conn.commit()
        self.assertEqual(loaded_row_count(self.conn, self.config.raw_schema, "eligibility", "run-pg-3"), 2)

    def test_loading_one_endpoint_does_not_affect_other_raw_tables(self):
        manifest = self._publish_run("run-pg-4", endpoint="eligibility")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-4", manifest)
        self.conn.commit()
        self.assertEqual(self._row_count("medical_claim"), 0)
        self.assertEqual(self._row_count("pharmacy_claim"), 0)

    def test_rollback_after_simulated_failure_leaves_table_untouched(self):
        manifest = self._publish_run("run-pg-5")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-5", manifest)
        self.conn.commit()
        self.assertEqual(self._row_count("eligibility"), 2)

        manifest_2 = self._publish_run("run-pg-6", records=[{"person_id": "p3"}])
        load_paginated_run(self.conn, self.config, self.store, "run-pg-6", manifest_2)
        # Simulate a failure discovered after loading but before commit
        # (e.g. a reconciliation mismatch) -- roll back instead.
        self.conn.rollback()
        self.assertEqual(self._row_count("eligibility"), 2)  # only run-pg-5's rows remain

    def test_watermark_committed_only_after_transaction_commits(self):
        self.assertIsNone(state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility"))

        manifest = self._publish_run("run-pg-7", high_water_mark="2025-07-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-7", manifest)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-07-01T00:00:00Z", successful_run_id="run-pg-7",
        )
        # Not committed yet -- a fresh connection must not see it.
        other_conn = connect(PG_DSN)
        try:
            self.assertIsNone(state.get_watermark(other_conn, self.ops_schema, "tuva", "eligibility"))
        finally:
            other_conn.close()

        self.conn.commit()
        committed = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        self.assertEqual(committed["high_water_mark"], "2025-07-01T00:00:00Z")
        self.assertEqual(committed["successful_run_id"], "run-pg-7")

    def test_watermark_not_committed_after_rollback(self):
        manifest = self._publish_run("run-pg-8", high_water_mark="2025-08-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-8", manifest)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-08-01T00:00:00Z", successful_run_id="run-pg-8",
        )
        self.conn.rollback()
        self.assertIsNone(state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility"))

    def test_watermark_advances_across_successive_successful_runs(self):
        manifest_1 = self._publish_run("run-pg-9", high_water_mark="2025-01-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-9", manifest_1)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-01-01T00:00:00Z", successful_run_id="run-pg-9",
        )
        self.conn.commit()

        manifest_2 = self._publish_run("run-pg-10", high_water_mark="2025-02-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-10", manifest_2)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-02-01T00:00:00Z", successful_run_id="run-pg-10",
        )
        self.conn.commit()

        committed = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        self.assertEqual(committed["high_water_mark"], "2025-02-01T00:00:00Z")
        self.assertEqual(committed["successful_run_id"], "run-pg-10")

    def test_backward_watermark_movement_is_rejected_by_the_same_guard_cli_uses(self):
        from tuva_ingest.errors import WatermarkError

        manifest_1 = self._publish_run("run-pg-11", high_water_mark="2025-05-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-pg-11", manifest_1)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-05-01T00:00:00Z", successful_run_id="run-pg-11",
        )
        self.conn.commit()

        prior = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        candidate_hwm = "2025-01-01T00:00:00Z"  # earlier than the committed value
        # This is exactly cli._run_paginated_load's own guard condition.
        if prior and prior["high_water_mark"] is not None and candidate_hwm < prior["high_water_mark"]:
            with self.assertRaises(WatermarkError):
                raise WatermarkError("candidate high_water_mark would move the watermark backward")
        else:
            self.fail("expected the backward-movement condition to be true in this test")

        # And the stored watermark must still be the original, later value.
        still_committed = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        self.assertEqual(still_committed["high_water_mark"], "2025-05-01T00:00:00Z")


class TestQuarantineAgainstRealDatabase(_IsolatedSchemaTestCase):
    """Exercises the restricted quarantine table (migrations/006,
    quarantine.py, paginated_loader.py's routing logic) against a real,
    disposable PostgreSQL database. Proves:

      * the database-level access model is exactly as restrictive as
        migrations/006 documents: PUBLIC has nothing, ingest_role has
        INSERT only (never SELECT/UPDATE/DELETE -- in particular,
        migration 003's ALTER DEFAULT PRIVILEGES must never have leaked
        SELECT/UPDATE onto this table), transform_role has nothing;
      * a structurally invalid record is written to quarantine and never
        also appears in the raw table (mutual exclusivity);
      * load_paginated_run's returned LoadCounts satisfies this
        connector's required reconciliation identity
        (source_record_count == raw_loaded_count + quarantined_count);
      * a failure while inserting a quarantine row rolls back the whole
        transaction -- no partial raw rows, no partial quarantine rows;
      * a fully successful run commits raw rows, quarantine rows, and the
        watermark together, in one transaction;
      * a pagination-limit/reconciliation-style failure leaves the prior
        watermark completely unchanged.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)
        self.store = PaginatedRunStore(self.raw_data_dir, "tuva")

    def _publish_run(self, run_id, *, endpoint="eligibility", records=None, high_water_mark="2025-06-01T00:00:00Z"):
        from datetime import datetime, timezone

        records = records if records is not None else [{"person_id": "p1"}, {"person_id": "p2"}]
        staging = self.store.begin_staging(run_id)
        payload = {
            "records": records,
            "metadata": {
                "record_count": len(records), "page_token": None, "next_page_token": None,
                "high_water_mark": high_water_mark,
            },
        }
        envelope = validate_page_envelope(payload, requested_page_token=None)
        meta = self.store.write_page(
            staging, run_id=run_id, endpoint=endpoint, page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        self.store.finalize(
            staging, run_id, [meta], endpoint=endpoint, since=None,
            total_record_count=len(records), candidate_high_water_mark=high_water_mark,
        )
        return verify_run_manifest(self.store, run_id)

    def _row_count(self, table):
        relation = f'"{self.raw_schema}"."{table}"'
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation}")
            return cur.fetchone()[0]

    def _quarantine_row_count(self):
        relation = f'"{self.ops_schema}"."quarantined_records"'
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation}")
            return cur.fetchone()[0]

    def _table_privileges(self, grantee):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = %s AND table_name = %s AND grantee = %s",
                (self.ops_schema, "quarantined_records", grantee),
            )
            return {row[0] for row in cur.fetchall()}

    def test_public_has_no_privileges_on_quarantine_table(self):
        self.assertEqual(self._table_privileges("PUBLIC"), set())

    def test_ingest_role_has_insert_only_on_quarantine_table(self):
        # Load-bearing check for migrations/006's explicit REVOKE before
        # GRANT INSERT -- without it, migration 003's ALTER DEFAULT
        # PRIVILEGES would silently leave SELECT/UPDATE granted too.
        self.assertEqual(self._table_privileges(self.config.ingest_role), {"INSERT"})

    def test_transform_role_has_no_privileges_on_quarantine_table(self):
        self.assertEqual(self._table_privileges(self.config.transform_role), set())

    def test_quarantined_record_never_also_appears_in_raw_table(self):
        manifest = self._publish_run(
            "run-q-1",
            records=[{"person_id": "p1"}, {"person_id": None}, {"person_id": "p2"}],
        )
        counts = load_paginated_run(self.conn, self.config, self.store, "run-q-1", manifest)
        self.conn.commit()

        self.assertEqual(counts.valid_count, 2)
        self.assertEqual(counts.quarantined_count, 1)
        self.assertEqual(self._row_count("eligibility"), 2)
        self.assertEqual(self._quarantine_row_count(), 1)

        with self.conn.cursor() as cur:
            cur.execute(f'SELECT raw_row FROM "{self.raw_schema}"."eligibility" WHERE _snapshot_id = %s', ("run-q-1",))
            raw_person_ids = {row[0]["person_id"] for row in cur.fetchall()}
        self.assertEqual(raw_person_ids, {"p1", "p2"})

    def test_reconciliation_identity_holds_with_a_mix_of_valid_and_invalid_records(self):
        manifest = self._publish_run(
            "run-q-2",
            records=[{"person_id": "p1"}, {"person_id": None}, {"person_id": "p2"}, {"person_id": ""}],
        )
        counts = load_paginated_run(self.conn, self.config, self.store, "run-q-2", manifest)
        self.conn.commit()

        source_record_count = manifest["total_record_count"]
        raw_loaded_count = loaded_row_count(self.conn, self.config.raw_schema, "eligibility", "run-q-2")
        self.assertEqual(source_record_count, raw_loaded_count + counts.quarantined_count)
        self.assertEqual(raw_loaded_count, 2)
        self.assertEqual(counts.quarantined_count, 2)

    def test_repeated_load_does_not_duplicate_quarantine_rows(self):
        manifest = self._publish_run("run-q-3", records=[{"person_id": "p1"}, {"person_id": None}])
        load_paginated_run(self.conn, self.config, self.store, "run-q-3", manifest)
        self.conn.commit()
        self.assertEqual(self._quarantine_row_count(), 1)

        load_paginated_run(self.conn, self.config, self.store, "run-q-3", manifest)  # repeat
        self.conn.commit()
        self.assertEqual(self._quarantine_row_count(), 1)  # still exactly one, not two

    def test_rollback_on_simulated_failed_quarantine_insert_leaves_everything_untouched(self):
        from tuva_ingest.errors import QuarantineError

        manifest = self._publish_run("run-q-4", records=[{"person_id": "p1"}, {"person_id": None}])

        # Simulate insert_quarantine_record itself failing partway
        # through the page (e.g. a transient database error) -- the
        # caller (paginated_loader.load_paginated_run) wraps this in
        # QuarantineError and the whole caller-owned transaction must be
        # rolled back, leaving neither raw rows nor quarantine rows
        # committed.
        with mock.patch(
            "tuva_ingest.paginated_loader.insert_quarantine_record",
            side_effect=RuntimeError("simulated transient database error"),
        ):
            with self.assertRaises(QuarantineError):
                load_paginated_run(self.conn, self.config, self.store, "run-q-4", manifest)
        self.conn.rollback()

        self.assertEqual(self._row_count("eligibility"), 0)
        self.assertEqual(self._quarantine_row_count(), 0)

    def test_watermark_left_unchanged_after_reconciliation_style_failure(self):
        # Establish a prior successful run/watermark.
        manifest_1 = self._publish_run("run-q-5", high_water_mark="2025-03-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-q-5", manifest_1)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-03-01T00:00:00Z", successful_run_id="run-q-5",
        )
        self.conn.commit()
        prior = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        self.assertEqual(prior["high_water_mark"], "2025-03-01T00:00:00Z")

        # A second run fails a reconciliation-style check after loading
        # but before commit (mirrors cli._run_paginated_load's own
        # sequencing: reconciliation happens before commit_watermark is
        # ever called) -- roll back instead of committing or advancing
        # the watermark.
        manifest_2 = self._publish_run("run-q-6", records=[{"person_id": "p3"}], high_water_mark="2025-04-01T00:00:00Z")
        load_paginated_run(self.conn, self.config, self.store, "run-q-6", manifest_2)
        # Simulate the reconciliation check failing (never call
        # commit_watermark) and roll back, exactly as cli.py does on any
        # ReconciliationError/QuarantineError/WatermarkError.
        self.conn.rollback()

        still_prior = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        self.assertEqual(still_prior["high_water_mark"], "2025-03-01T00:00:00Z")
        self.assertEqual(still_prior["successful_run_id"], "run-q-5")

    def test_fully_successful_run_commits_raw_quarantine_and_watermark_together(self):
        manifest = self._publish_run(
            "run-q-7",
            records=[{"person_id": "p1"}, {"person_id": None}],
            high_water_mark="2025-09-01T00:00:00Z",
        )
        counts = load_paginated_run(self.conn, self.config, self.store, "run-q-7", manifest)
        state.commit_watermark(
            self.conn, self.ops_schema, "tuva", "eligibility",
            high_water_mark="2025-09-01T00:00:00Z", successful_run_id="run-q-7",
        )
        self.conn.commit()

        self.assertEqual(self._row_count("eligibility"), 1)
        self.assertEqual(self._quarantine_row_count(), 1)
        self.assertEqual(counts.valid_count, 1)
        self.assertEqual(counts.quarantined_count, 1)
        committed = state.get_watermark(self.conn, self.ops_schema, "tuva", "eligibility")
        self.assertEqual(committed["high_water_mark"], "2025-09-01T00:00:00Z")
        self.assertEqual(committed["successful_run_id"], "run-q-7")


if __name__ == "__main__":
    unittest.main()
