#!/usr/bin/env python3
"""Maintenance utility for tests/fixtures/ci/complete_snapshot/.

Regenerates (or verifies) the committed deterministic CI fixture by
introspecting a REAL, migrated, DISPOSABLE PostgreSQL schema --
information_schema.columns (ordered by ordinal_position) rather than a
second, hand-maintained copy of each managed table's column list. This is
what keeps the fixture from silently drifting out of sync with migration
0001's baseline DDL (db/migrations/sql/0001_baseline/core/*.sql) the next
time a column is added, renamed, or reordered.

*** SAFETY ***
This script NEVER connects to a database unless you explicitly pass
--pg-dsn (or set PG_DSN) AND --pg-schema (or set PG_SCHEMA) -- there is
no default DSN and no default schema. It refuses reserved/production-like
schema names (public, tuva, tuva_term, tuva_ops) so it can never be
pointed at a real deployment's data by accident. Point it at a disposable
schema in a disposable database only -- e.g. one created by
`make local-db-ready` or a throwaway CI Postgres service -- never a
shared or production database.

This script is read-only against the database: it only ever runs SELECT
statements against information_schema/pg_catalog. It never writes to the
database, and it never overwrites the committed fixture unless you pass
--out tests/fixtures/ci/complete_snapshot explicitly (the default output
directory is a fresh temporary directory).

*** WHAT THIS PROVES / DOES NOT PROVE ***
Regenerating output that differs from the committed fixture means the
migrated schema has drifted from what the fixture assumes (a column was
added, removed, renamed, or reordered) -- not that the committed fixture
is "wrong" per se. Review the diff, decide whether the new/changed
column needs a deterministic value (edit OVERRIDES below), regenerate,
and commit the updated fixture in the same change as the schema/DDL edit
that caused the drift.

Modes:
  generate   Write one CSV per tuva_postgres.manifest.MANAGED_TABLES table
             to --out (a fresh temp dir by default; pass
             --out tests/fixtures/ci/complete_snapshot to actually update
             the committed fixture -- an explicit, deliberate opt-in).
  check      Generate into a temporary directory and diff every file
             against the committed tests/fixtures/ci/complete_snapshot/
             (or --fixture-dir). Exits nonzero and prints a per-table diff
             summary if anything differs -- e.g. from a CI job wired to
             catch fixture drift after a schema change, run manually
             against a disposable database.

CI never runs this script before loading the fixture -- CI loads the
already-committed fixture as-is (see scripts/tests/test_ci_complete_run.sh
and .github/workflows/ci.yml). Regenerating immediately before load would
hide exactly the kind of schema drift this script exists to catch.

Usage (requires a disposable, already-migrated PostgreSQL schema):
  PG_DSN=postgresql://user:pass@host:port/db uv run python3 \\
    scripts/generate_ci_fixture.py check --pg-schema my_disposable_schema

  PG_DSN=postgresql://user:pass@host:port/db uv run python3 \\
    scripts/generate_ci_fixture.py generate --pg-schema my_disposable_schema \\
    --out tests/fixtures/ci/complete_snapshot
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.manifest import MANAGED_TABLES  # noqa: E402

RESERVED_SCHEMA_NAMES = {
    "public",
    "tuva",
    "tuva_term",
    "tuva_ops",
    "information_schema",
    "pg_catalog",
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ci" / "complete_snapshot"

# --- Deterministic values -----------------------------------------------
# Fixed synthetic identifiers/dates shared across tables so referential
# relationships (patient <-> encounter <-> practitioner/location, plus
# every dependent clinical/claims table) are non-vacuous. Kept in exact
# sync with the hand-authored committed fixture -- `check` mode's whole
# purpose is to prove that sync hasn't drifted.
PERSON_ID = "person-1"
PRACTITIONER_ID = "practitioner-1"
LOCATION_ID = "location-1"
ENCOUNTER_ID = "encounter-1"
FIXED_DATE = "2024-01-15"
FIXED_TS = "2024-01-15 00:00:00"

# One deterministic, schema-valid, synthetic row per managed table, keyed
# by column name -- NOT by ordinal position, so this survives a column
# reorder unchanged (only additions/removals/renames require an edit
# here). Any column not listed here (including any newly added column
# this dict doesn't yet know about) is left blank (NULL after the
# loader's `NULL ''` \copy option), matching every nullable column's
# default in the DDL. Primary keys are filled in by `_pk_value()` below,
# not listed per-table here.
OVERRIDES: dict[str, dict[str, str]] = {
    "practitioner": {
        "npi": "1234567893",
        "provider_first_name": "Terry",
        "provider_last_name": "Practitioner",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "location": {
        "npi": "1000000004",
        "name": "CI Fixture Clinic",
        "city": "San Francisco",
        "state": "CA",
        "zip_code": "94105",
        "latitude": "37.7749",
        "longitude": "-122.4194",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "patient": {
        "person_id": PERSON_ID,
        "first_name": "Pat",
        "last_name": "Patient",
        "birth_date": "1980-01-01",
        "death_flag": "0",
        "city": "San Francisco",
        "state": "CA",
        "zip_code": "94105",
        "latitude": "37.7749",
        "longitude": "-122.4194",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "encounter": {
        "encounter_id": ENCOUNTER_ID,
        "person_id": PERSON_ID,
        "encounter_start_date": "2024-01-10",
        "encounter_end_date": "2024-01-12",
        "length_of_stay": "2",
        "attending_provider_id": PRACTITIONER_ID,
        "facility_id": LOCATION_ID,
        "observation_flag": "1",
        "lab_flag": "1",
        "dme_flag": "0",
        "ambulance_flag": "0",
        "pharmacy_flag": "0",
        "ed_flag": "0",
        "delivery_flag": "0",
        "newborn_flag": "0",
        "nicu_flag": "0",
        "snf_part_b_flag": "0",
        "paid_amount": "100.00",
        "allowed_amount": "150.00",
        "charge_amount": "200.00",
        "claim_count": "1",
        "inst_claim_count": "1",
        "prof_claim_count": "0",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "person_id_crosswalk": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "member_id": "member-1",
        "data_source": "ci-fixture",
    },
    "medical_claim": {
        "claim_id": "claim-1",
        "claim_line_number": "1",
        "encounter_id": ENCOUNTER_ID,
        "person_id": PERSON_ID,
        "claim_start_date": "2024-01-10",
        "claim_end_date": "2024-01-12",
        "claim_line_start_date": "2024-01-10",
        "claim_line_end_date": "2024-01-12",
        "admission_date": "2024-01-10",
        "discharge_date": "2024-01-12",
        "service_unit_quantity": "1",
        "facility_id": LOCATION_ID,
        "paid_date": "2024-01-20",
        "paid_amount": "100.00",
        "allowed_amount": "150.00",
        "charge_amount": "200.00",
        "coinsurance_amount": "10.00",
        "copayment_amount": "20.00",
        "deductible_amount": "20.00",
        "total_cost_amount": "200.00",
        "in_network_flag": "1",
        "enrollment_flag": "1",
        "data_source": "ci-fixture",
        "file_date": FIXED_DATE,
        "tuva_last_run": FIXED_TS,
    },
    "pharmacy_claim": {
        "claim_id": "rx-claim-1",
        "claim_line_number": "1",
        "person_id": PERSON_ID,
        "dispensing_date": "2024-01-10",
        "quantity": "30",
        "days_supply": "30",
        "refills": "0",
        "paid_date": FIXED_DATE,
        "paid_amount": "10.00",
        "allowed_amount": "15.00",
        "charge_amount": "20.00",
        "coinsurance_amount": "2.00",
        "copayment_amount": "3.00",
        "deductible_amount": "0.00",
        "in_network_flag": "1",
        "enrollment_flag": "1",
        "data_source": "ci-fixture",
        "file_date": FIXED_DATE,
        "tuva_last_run": FIXED_TS,
    },
    "eligibility": {
        "person_id": PERSON_ID,
        "member_id": "member-1",
        "birth_date": "1980-01-01",
        "enrollment_start_date": "2024-01-01",
        "enrollment_end_date": "2024-12-31",
        "data_source": "ci-fixture",
        "file_date": FIXED_DATE,
        "tuva_last_run": FIXED_TS,
    },
    "procedure": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "encounter_id": ENCOUNTER_ID,
        "procedure_date": "2024-01-11",
        "practitioner_id": PRACTITIONER_ID,
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "observation": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "encounter_id": ENCOUNTER_ID,
        "observation_date": "2024-01-11",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "lab_result": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "encounter_id": ENCOUNTER_ID,
        "result_datetime": "2024-01-11 10:00:00",
        "collection_datetime": "2024-01-11 08:00:00",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "condition": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "encounter_id": ENCOUNTER_ID,
        "recorded_date": "2024-01-11",
        "onset_date": "2024-01-10",
        "condition_rank": "1",
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "medication": {
        "person_id": PERSON_ID,
        "encounter_id": ENCOUNTER_ID,
        "dispensing_date": "2024-01-11",
        "prescribing_date": "2024-01-10",
        "quantity": "30",
        "days_supply": "30",
        "practitioner_id": PRACTITIONER_ID,
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
    "immunization": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "encounter_id": ENCOUNTER_ID,
        "occurrence_date": "2024-01-11",
        "location_id": LOCATION_ID,
        "practitioner_id": PRACTITIONER_ID,
        "data_source": "ci-fixture",
        "ingest_datetime": FIXED_TS,
    },
    "appointment": {
        "person_id": PERSON_ID,
        "patient_id": "patient-1",
        "encounter_id": ENCOUNTER_ID,
        "start_datetime": "2024-01-09 09:00:00",
        "end_datetime": "2024-01-09 09:30:00",
        "duration": "30.00",
        "location_id": LOCATION_ID,
        "practitioner_id": PRACTITIONER_ID,
        "data_source": "ci-fixture",
        "tuva_last_run": FIXED_TS,
    },
}

# Primary key column per managed table -> deterministic value. Every
# other table's PK is simply "{table}-1"; called out explicitly (not
# derived from a naming convention) so a future PK rename can't silently
# leave a table's key column blank.
_PK_VALUES = {
    "practitioner": ("practitioner_id", PRACTITIONER_ID),
    "location": ("location_id", LOCATION_ID),
    "patient": ("person_id", PERSON_ID),
    "encounter": ("encounter_id", ENCOUNTER_ID),
    "medical_claim": ("medical_claim_id", "medical_claim-1"),
    "pharmacy_claim": ("pharmacy_claim_id", "pharmacy_claim-1"),
    "eligibility": ("eligibility_id", "eligibility-1"),
    "procedure": ("procedure_id", "procedure-1"),
    "observation": ("observation_id", "observation-1"),
    "lab_result": ("lab_result_id", "lab_result-1"),
    "condition": ("condition_id", "condition-1"),
    "medication": ("medication_id", "medication-1"),
    "immunization": ("immunization_id", "immunization-1"),
    "appointment": ("appointment_id", "appointment-1"),
    # person_id_crosswalk has no single-column primary key (see
    # db/migrations/sql/0001_baseline/core/person_id_crosswalk.sql) --
    # its person_id is supplied via OVERRIDES above instead.
}


def _require_schema_name(name: str | None, *, source: str) -> str:
    if not name:
        raise SystemExit(
            f"ERROR: no PG_SCHEMA supplied ({source}). This script refuses to guess a schema "
            "name -- pass --pg-schema explicitly (or set PG_SCHEMA) and point it at a "
            "disposable schema in a disposable database."
        )
    if not _IDENTIFIER_RE.match(name):
        raise SystemExit(f"ERROR: --pg-schema={name!r} is not a safe SQL identifier.")
    if name.lower() in RESERVED_SCHEMA_NAMES:
        raise SystemExit(
            f"ERROR: --pg-schema={name!r} is a reserved/production-like schema name "
            f"({sorted(RESERVED_SCHEMA_NAMES)}). Point this at a disposable schema created "
            "for this purpose only, e.g. 'ci_fixture_check_<random>'."
        )
    return name


def _require_dsn(dsn: str | None) -> str:
    if not dsn:
        raise SystemExit(
            "ERROR: no PG_DSN supplied. This script never connects to a database unless you "
            "explicitly pass --pg-dsn (or set PG_DSN) pointing at a disposable database. "
            "There is no default connection target."
        )
    return dsn


def _columns(conn, pg_schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (pg_schema, table),
        )
        cols = [r[0] for r in cur.fetchall()]
    if not cols:
        raise SystemExit(
            f"ERROR: no columns found for {pg_schema}.{table} -- has migration 0001 been "
            "applied to this schema? (see `make migrate` / `uv run tuva-postgres migrate`)"
        )
    return cols


def _row_for(table: str, columns: list[str]) -> list[str]:
    overrides = OVERRIDES.get(table, {})
    pk_col, pk_val = _PK_VALUES.get(table, (None, None))
    values = []
    for col in columns:
        if col == pk_col:
            values.append(pk_val)
        elif col in overrides:
            values.append(overrides[col])
        else:
            values.append("")
    return values


def _write_csv(path: Path, columns: list[str], row: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        writer.writerow(row)


def _generate(conn, pg_schema: str, out_dir: Path) -> None:
    for table in MANAGED_TABLES:
        columns = _columns(conn, pg_schema, table)
        row = _row_for(table, columns)
        _write_csv(out_dir / f"{table}.csv", columns, row)
        print(f"wrote {out_dir / f'{table}.csv'} ({len(columns)} column(s))")


def _diff_dirs(generated_dir: Path, committed_dir: Path) -> list[str]:
    problems = []
    for table in MANAGED_TABLES:
        gen_path = generated_dir / f"{table}.csv"
        committed_path = committed_dir / f"{table}.csv"
        if not committed_path.is_file():
            problems.append(f"{table}: committed fixture file is missing: {committed_path}")
            continue
        gen_text = gen_path.read_text(encoding="utf-8")
        committed_text = committed_path.read_text(encoding="utf-8")
        if gen_text != committed_text:
            gen_lines = gen_text.splitlines()
            committed_lines = committed_text.splitlines()
            problems.append(
                f"{table}: committed fixture has drifted from the migrated schema.\n"
                f"    schema-introspected header: {gen_lines[0] if gen_lines else '<empty>'}\n"
                f"    committed fixture header:   {committed_lines[0] if committed_lines else '<empty>'}"
            )
    extra_committed = sorted(
        p.name
        for p in committed_dir.glob("*.csv")
        if p.stem not in MANAGED_TABLES
    )
    if extra_committed:
        problems.append(f"committed fixture has unexpected file(s) not in MANAGED_TABLES: {extra_committed}")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["generate", "check"])
    parser.add_argument("--pg-dsn", default=os.environ.get("PG_DSN"))
    parser.add_argument("--pg-schema", default=os.environ.get("PG_SCHEMA"))
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for 'generate' (default: a fresh temp dir -- pass "
        "tests/fixtures/ci/complete_snapshot explicitly to update the committed fixture).",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=DEFAULT_FIXTURE_DIR,
        help="Committed fixture directory to compare against in 'check' mode.",
    )
    args = parser.parse_args(argv)

    dsn = _require_dsn(args.pg_dsn)
    pg_schema = _require_schema_name(args.pg_schema, source="--pg-schema/PG_SCHEMA")

    try:
        import psycopg
    except ImportError:
        raise SystemExit(
            "ERROR: psycopg is required (run `uv sync --locked`, then `uv run python3 "
            "scripts/generate_ci_fixture.py ...`)."
        ) from None

    with psycopg.connect(dsn) as conn:
        if args.mode == "generate":
            out_dir = args.out or Path(tempfile.mkdtemp(prefix="tuva-ci-fixture-"))
            if out_dir == args.fixture_dir:
                print(
                    f"NOTE: writing directly to the committed fixture directory ({out_dir}) -- "
                    "review the resulting `git diff` carefully before committing.",
                    file=sys.stderr,
                )
            _generate(conn, pg_schema, out_dir)
            print(f"\nGenerated {len(MANAGED_TABLES)} CSV(s) under {out_dir}")
            return 0

        # check mode: always generate into a private temp dir, never touch --out
        tmp_dir = Path(tempfile.mkdtemp(prefix="tuva-ci-fixture-check-"))
        _generate(conn, pg_schema, tmp_dir)
        problems = _diff_dirs(tmp_dir, args.fixture_dir)
        if problems:
            print(
                f"FAIL: the committed fixture ({args.fixture_dir}) has drifted from schema "
                f"{pg_schema!r} as migrated in this database:\n",
                file=sys.stderr,
            )
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print(
                "\nRegenerate with:\n"
                f"  PG_DSN=... uv run python3 scripts/generate_ci_fixture.py generate "
                f"--pg-schema {pg_schema} --out {args.fixture_dir}\n"
                "then review the diff, add any new column(s) needing a deterministic value to "
                "OVERRIDES in this script, and commit the fixture together with the schema change "
                "that caused the drift.",
                file=sys.stderr,
            )
            return 1
        print(f"PASS: committed fixture at {args.fixture_dir} matches schema {pg_schema!r} exactly.")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
