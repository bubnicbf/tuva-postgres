#!/usr/bin/env python3
"""Reusable verifier for a complete migrate -> load -> test run against a
real PostgreSQL database. Called after `scripts/load_to_postgres.sh` and
`scripts/run_tests.sh` have both run (see `make test-ci-complete-run` and
.github/workflows/ci.yml) to prove -- from evidence actually written to
the database, not just "the previous steps exited zero" -- that:

  * every managed table exists and has exactly its expected row count
    from the committed fixture (tests/fixtures/ci/complete_snapshot/ by
    default) -- proving the loader did not take its zero-file no-op path
    and did not partially load;
  * the SQL validation suite (db/tests/*.sql, run through
    scripts/run_tests.sh) actually executed and wrote result rows tied to
    the exact RUN_ID this run used -- not a stale row left over from some
    earlier run;
  * every expected result-producing SQL suite (every db/tests/*.sql file
    except the zz_results.sql setup file) is represented in those
    results;
  * not one of those results has pass = false;
  * migration status is current: no pending migration, no checksum
    mismatch, no execution-mode mismatch (via the real
    tuva_postgres.migrations.status() API -- the same function
    `tuva-postgres migrate --status` calls).

Configuration (env var, with an equivalent --flag override):
  PG_DSN          (required) -- must point at a disposable database.
  PG_SCHEMA       (required)
  OPS_SCHEMA      (default: tuva_ops)
  RUN_ID          (required) -- the exact run id scripts/run_tests.sh was
                  invoked with (RUN_ID=... bash scripts/run_tests.sh).
  FIXTURE_DIR     (default: tests/fixtures/ci/complete_snapshot) -- used
                  only to compute each managed table's expected row
                  count by counting that table's committed CSV's data
                  rows; never read or written otherwise.

PG_DSN is never printed or logged by this script.

Exit status is nonzero unless every piece of evidence above is present
and correct; prints a concise, CI-friendly report either way.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.identifiers import InvalidIdentifierError, validate_identifier  # noqa: E402
from tuva_postgres.manifest import MANAGED_TABLES  # noqa: E402

DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ci" / "complete_snapshot"
SQL_TEST_DIR = REPO_ROOT / "db" / "tests"
RESULTS_SETUP_SQL_NAME = "zz_results.sql"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class VerificationError(Exception):
    """Raised for any piece of missing/incorrect evidence, naming exactly
    what was expected vs. what was found."""


def _require(value: str | None, *, flag: str, env: str) -> str:
    if not value:
        raise SystemExit(f"ERROR: {env} not set (or pass {flag}). This script requires it explicitly.")
    return value


def _expected_row_counts(fixture_dir: Path) -> dict[str, int]:
    if not fixture_dir.is_dir():
        raise VerificationError(f"fixture directory not found: {fixture_dir}")
    counts = {}
    for table in MANAGED_TABLES:
        path = fixture_dir / f"{table}.csv"
        if not path.is_file():
            raise VerificationError(f"fixture directory {fixture_dir} is missing {table}.csv")
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        data_rows = [r for r in rows[1:] if r]
        counts[table] = len(data_rows)
    return counts


def _expected_suites() -> list[str]:
    """Every db/tests/*.sql file except the results-table setup file,
    mirroring scripts/run_tests.sh's own discovery -- as
    "{basename}.csv" suite names, matching scripts/ingest_test_csv.py's
    suite naming (os.path.basename of the per-suite CSV it produced)."""
    if not SQL_TEST_DIR.is_dir():
        raise VerificationError(f"SQL test directory not found: {SQL_TEST_DIR}")
    suites = []
    for path in sorted(SQL_TEST_DIR.glob("*.sql")):
        if path.name == RESULTS_SETUP_SQL_NAME:
            continue
        suites.append(f"{path.stem}.csv")
    if not suites:
        raise VerificationError(f"no SQL validation suites found under {SQL_TEST_DIR}")
    return suites


def _validate_identifier(name: str, value: str) -> str:
    try:
        return validate_identifier(value, name)
    except InvalidIdentifierError as exc:
        raise SystemExit(f"ERROR: {exc}") from None


def run(pg_dsn: str, pg_schema: str, ops_schema: str, run_id: str, fixture_dir: Path) -> list[str]:
    if not _RUN_ID_RE.match(run_id):
        raise VerificationError(
            f"RUN_ID={run_id!r} is not safely formatted (expected up to 128 chars of "
            "letters/digits/._- , starting with a letter or digit)"
        )
    _validate_identifier("PG_SCHEMA", pg_schema)
    _validate_identifier("OPS_SCHEMA", ops_schema)

    expected_counts = _expected_row_counts(fixture_dir)
    expected_suites = _expected_suites()

    try:
        import psycopg
    except ImportError:
        raise SystemExit(
            "ERROR: psycopg is required (run `uv sync --locked`, then `uv run python3 "
            "scripts/verify_complete_run.py ...`)."
        ) from None

    from tuva_postgres import db, migrations
    from tuva_postgres.config import REQUIRE_DB, PipelineConfig

    report: list[str] = [f"RUN_ID: {run_id}"]

    os.environ["PG_DSN"] = pg_dsn
    os.environ["PG_SCHEMA"] = pg_schema
    os.environ["OPS_SCHEMA"] = ops_schema
    config = PipelineConfig.load(required=REQUIRE_DB)

    with db.connect(pg_dsn) as conn:
        # --- 1) every managed table exists with its exact expected count ---
        with conn.cursor() as cur:
            table_report = []
            for table in MANAGED_TABLES:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                    (pg_schema, table),
                )
                if cur.fetchone() is None:
                    raise VerificationError(f"managed table {pg_schema}.{table} does not exist")

                relation = db.qualified_relation(pg_schema, table, schema_label="PG_SCHEMA", relation_label="table")
                cur.execute(f"SELECT COUNT(*) FROM {relation}")
                (actual_count,) = cur.fetchone()
                expected_count = expected_counts[table]
                if actual_count != expected_count:
                    raise VerificationError(
                        f"{pg_schema}.{table}: has {actual_count} row(s), expected exactly "
                        f"{expected_count} (from the committed fixture) -- either the loader's "
                        "zero-file no-op path was taken, the load was partial, or a prior run's "
                        "data was never replaced"
                    )
                table_report.append(f"{table}={actual_count}")
        report.append("Managed table row counts (all match the committed fixture exactly, so the")
        report.append("loader did not take its zero-file no-op path): " + ", ".join(table_report))

        # --- 2) test_results has rows for this exact RUN_ID --------------------
        test_results = db.qualified_relation(pg_schema, "test_results", schema_label="PG_SCHEMA")
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT suite, pass, COUNT(*) FROM {test_results} "
                "WHERE run_id = %s GROUP BY suite, pass ORDER BY suite, pass",
                (run_id,),
            )
            rows = cur.fetchall()

        if not rows:
            raise VerificationError(
                f"{pg_schema}.test_results has no row(s) for run_id={run_id!r} -- the SQL "
                "validation suite did not execute, was skipped, or was run with a different "
                "RUN_ID than this verifier was given"
            )

        total = passed = failed = 0
        suites_seen: dict[str, dict[str, int]] = {}
        for suite, is_pass, count in rows:
            suites_seen.setdefault(suite, {"pass": 0, "fail": 0})
            suites_seen[suite]["pass" if is_pass else "fail"] += count
            total += count
            if is_pass:
                passed += count
            else:
                failed += count

        report.append(f"SQL validation results for run_id={run_id}: total={total}, passed={passed}, failed={failed}")

        if failed > 0:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT suite, test FROM {test_results} "
                    "WHERE run_id = %s AND pass = false ORDER BY suite, test LIMIT 50",
                    (run_id,),
                )
                failures = cur.fetchall()
            failure_list = "; ".join(f"{s}:{t}" for s, t in failures)
            raise VerificationError(
                f"{failed} SQL validation result(s) for run_id={run_id!r} have pass = false: {failure_list}"
            )

        missing_suites = sorted(set(expected_suites) - set(suites_seen))
        if missing_suites:
            raise VerificationError(
                f"expected SQL suite(s) not represented in test_results for run_id={run_id!r}: "
                f"{missing_suites} (expected all {len(expected_suites)}: {expected_suites})"
            )
        report.append(f"Represented suites: {len(suites_seen)} of {len(expected_suites)} expected.")

        # --- 3) migration history + status are current --------------------------
        status = migrations.status(conn, config)
        if status.has_integrity_failures:
            raise VerificationError(
                f"migration status has integrity failures: one_time_mismatches="
                f"{status.one_time_mismatches!r}, mode_mismatches={status.mode_mismatches!r}"
            )
        if status.pending:
            pending_versions = [m.version for m in status.pending]
            raise VerificationError(f"migration status has pending migration(s): {pending_versions}")

        applied_versions = sorted(m.version for m in status.applied_one_time) + sorted(
            m.version for m in status.applied_repeatable_current
        )
        if not applied_versions:
            raise VerificationError("migration status reports no applied migrations at all")
        report.append("Migration status: current, no pending work, no integrity failures.")
        report.append(f"Applied migrations: {sorted(applied_versions)}")

    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pg-dsn", default=os.environ.get("PG_DSN"))
    parser.add_argument("--pg-schema", default=os.environ.get("PG_SCHEMA"))
    parser.add_argument("--ops-schema", default=os.environ.get("OPS_SCHEMA", "tuva_ops"))
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    parser.add_argument("--fixture-dir", type=Path, default=Path(os.environ.get("FIXTURE_DIR", str(DEFAULT_FIXTURE_DIR))))
    args = parser.parse_args(argv)

    pg_dsn = _require(args.pg_dsn, flag="--pg-dsn", env="PG_DSN")
    pg_schema = _require(args.pg_schema, flag="--pg-schema", env="PG_SCHEMA")
    run_id = _require(args.run_id, flag="--run-id", env="RUN_ID")

    try:
        report = run(pg_dsn, pg_schema, args.ops_schema, run_id, args.fixture_dir)
    except VerificationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print("PASS: complete migrate -> load -> test run verified end to end.")
    for line in report:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
