#!/usr/bin/env python3
"""Database-free structural regression test for the committed CI fixture
under tests/fixtures/ci/complete_snapshot/ -- the deterministic, complete
synthetic snapshot CI loads through the real scripts/load_to_postgres.sh
(see scripts/tests/test_ci_complete_run.sh for the database-backed half
of this proof, and scripts/generate_ci_fixture.py for how to regenerate
the fixture safely when the baseline schema changes).

This test never requires PostgreSQL, psycopg, Docker, or network access,
and never imports third-party packages -- only tuva_postgres.manifest
(pure-Python, stdlib-only) for the authoritative MANAGED_TABLES list. It
proves the fixture is *structurally* complete and internally consistent;
it cannot prove the fixture actually loads or that every SQL validation
check passes against a real schema -- that's what
scripts/tests/test_ci_complete_run.sh is for.

Checks performed against tests/fixtures/ci/complete_snapshot/:
  * the directory exists;
  * it contains exactly the 15 expected "{table}.csv" files (one per
    tuva_postgres.manifest.MANAGED_TABLES entry) -- no table missing, no
    unexpected extra file;
  * every file is valid UTF-8, uses LF line endings with no bare CR, and
    ends with a trailing newline;
  * every file has a nonempty header with nonempty, unique column names;
  * every file has at least one data row, and every row (parsed with the
    csv module, not naive comma-splitting) has exactly as many fields as
    the header;
  * every managed table has exactly its expected deterministic row count
    (see EXPECTED_ROW_COUNTS);
  * the fixed hub identifiers (person-1/practitioner-1/location-1/
    encounter-1) are present where expected;
  * the core foreign-key relationships declared in KNOWN_FOREIGN_KEYS
    resolve across files (every non-blank child value exists as a parent
    primary key), so a broken relationship is caught here, database-free,
    before it would otherwise only surface as a failed SQL check in
    db/tests/*_smoke.sql;
  * no field value looks secret-shaped (password/token/api key/private
    key/etc.) or contains a US-SSN-shaped value;
  * no field value equals *this test run's* current date/time -- guards
    against someone swapping a fixed synthetic date for `date.today()` /
    `datetime.now()` in the future (the fixture's actual 2024 dates are
    unaffected; they never equal "today" for any run of this repository);
  * the fixture directory is not excluded by .gitignore;
  * .github/workflows/ci.yml points DATA_DIR at this canonical fixture
    directory (not the plain "data" directory) and no longer uses vague
    "if present" language for what is now a required, validated load.

Usage:
    python3 scripts/tests/test_ci_fixture.py [fixture_root] [repo_root]

With no arguments, validates the real committed fixture and CI workflow.
Positional arguments let negative-control checks (see
TestCiFixtureNegativeControls below, run automatically as part of
`main()`) point validation at scratch temporary copies without ever
touching the real, committed files.
"""
from __future__ import annotations

import csv
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.manifest import MANAGED_TABLES  # noqa: E402

FIXTURE_RELATIVE_DIR = Path("tests") / "fixtures" / "ci" / "complete_snapshot"
CI_WORKFLOW_RELATIVE = Path(".github") / "workflows" / "ci.yml"

EXPECTED_FILENAMES = {f"{t}.csv" for t in MANAGED_TABLES}

# Every managed table has exactly one deterministic fixture row (see this
# script's module docstring and scripts/generate_ci_fixture.py's
# OVERRIDES) -- an exact count, not just ">= 1", so a copy-paste that
# accidentally duplicated or dropped a row is caught here.
EXPECTED_ROW_COUNTS = {t: 1 for t in MANAGED_TABLES}

FIXED_HUB_IDS = {
    "patient": ("person_id", "person-1"),
    "practitioner": ("practitioner_id", "practitioner-1"),
    "location": ("location_id", "location-1"),
    "encounter": ("encounter_id", "encounter-1"),
}

# (child_table, child_column, parent_table, parent_pk_column) -- mirrors
# the DEFERRABLE FOREIGN KEYs declared in migration 0001's core DDL (see
# db/migrations/sql/0001_baseline/core/*.sql) and the *_fk_exists checks
# in db/tests/*_smoke.sql. Only non-blank child values are checked (every
# one of these columns is nullable in the DDL).
KNOWN_FOREIGN_KEYS = [
    ("encounter", "person_id", "patient", "person_id"),
    ("encounter", "attending_provider_id", "practitioner", "practitioner_id"),
    ("person_id_crosswalk", "person_id", "patient", "person_id"),
    ("medical_claim", "person_id", "patient", "person_id"),
    ("medical_claim", "encounter_id", "encounter", "encounter_id"),
    ("pharmacy_claim", "person_id", "patient", "person_id"),
    ("eligibility", "person_id", "patient", "person_id"),
    ("procedure", "person_id", "patient", "person_id"),
    ("procedure", "encounter_id", "encounter", "encounter_id"),
    ("procedure", "practitioner_id", "practitioner", "practitioner_id"),
    ("observation", "person_id", "patient", "person_id"),
    ("observation", "encounter_id", "encounter", "encounter_id"),
    ("lab_result", "person_id", "patient", "person_id"),
    ("lab_result", "encounter_id", "encounter", "encounter_id"),
    ("condition", "person_id", "patient", "person_id"),
    ("condition", "encounter_id", "encounter", "encounter_id"),
    ("medication", "person_id", "patient", "person_id"),
    ("medication", "encounter_id", "encounter", "encounter_id"),
    ("medication", "practitioner_id", "practitioner", "practitioner_id"),
    ("immunization", "person_id", "patient", "person_id"),
    ("immunization", "encounter_id", "encounter", "encounter_id"),
    ("immunization", "practitioner_id", "practitioner", "practitioner_id"),
    ("appointment", "person_id", "patient", "person_id"),
    ("appointment", "encounter_id", "encounter", "encounter_id"),
    ("appointment", "practitioner_id", "practitioner", "practitioner_id"),
]

_SECRET_LOOKALIKE_RE = re.compile(
    r"(password|passwd|secret|api[_-]?key|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)", re.IGNORECASE
)
_SSN_SHAPED_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_DATE_SHAPED_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class FixtureError(Exception):
    """Raised for a structural problem in the CI fixture, naming the
    specific file/row/column at fault."""


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FixtureError(f"{path}: not valid UTF-8 ({exc})") from None

    if b"\r" in raw:
        raise FixtureError(f"{path}: contains a carriage return -- expected LF-only line endings")
    if not text.endswith("\n"):
        raise FixtureError(f"{path}: does not end with a trailing newline")

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    rows = [r for r in rows if r]  # csv.reader can yield [] for a stray blank line
    if not rows:
        raise FixtureError(f"{path}: has no header row (file is empty)")
    header, data_rows = rows[0], rows[1:]

    if not header or any(not col.strip() for col in header):
        raise FixtureError(f"{path}: header contains an empty column name: {header!r}")
    if len(set(header)) != len(header):
        dupes = sorted({c for c in header if header.count(c) > 1})
        raise FixtureError(f"{path}: header contains duplicate column name(s): {dupes}")
    if not data_rows:
        raise FixtureError(f"{path}: has a header but no data row")
    for i, row in enumerate(data_rows, start=2):
        if len(row) != len(header):
            raise FixtureError(
                f"{path}:{i}: row has {len(row)} field(s), header has {len(header)} "
                f"({header!r} vs {row!r})"
            )
    return header, data_rows


def _validate_gitignore(fixture_dir: Path, repo_root: Path) -> None:
    gitignore_path = repo_root / ".gitignore"
    if not gitignore_path.is_file():
        return
    patterns = []
    for raw_line in gitignore_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)

    rel = fixture_dir.relative_to(repo_root)
    candidates = [str(rel).replace("\\", "/")] + [
        str(rel / f"{t}.csv").replace("\\", "/") for t in MANAGED_TABLES
    ]
    for pattern in patterns:
        p = pattern.strip("/")
        for candidate in candidates:
            # A reasonable, repo-scoped approximation of gitignore matching
            # (not a full implementation): a pattern with no "/" matches
            # any path component anywhere; a pattern containing "/" is
            # matched from the repo root (with or without a leading "/",
            # git treats those the same for a pattern that also contains
            # an interior "/").
            if "/" not in p:
                parts = candidate.split("/")
                if any(Path(part).match(p) for part in parts):
                    raise FixtureError(
                        f"{candidate}: matches .gitignore pattern {pattern!r} -- the CI fixture "
                        "must be trackable by git, not ignored"
                    )
            elif candidate == p or candidate.startswith(p.rstrip("/") + "/"):
                raise FixtureError(
                    f"{candidate}: matches .gitignore pattern {pattern!r} -- the CI fixture "
                    "must be trackable by git, not ignored"
                )


def _validate_ci_workflow(repo_root: Path) -> list[str]:
    ci_path = repo_root / CI_WORKFLOW_RELATIVE
    if not ci_path.is_file():
        raise FixtureError(f"CI workflow not found: {ci_path}")
    text = ci_path.read_text(encoding="utf-8")

    fixture_ref = "tests/fixtures/ci/complete_snapshot"
    if fixture_ref not in text:
        raise FixtureError(
            f"{ci_path}: does not reference the canonical fixture directory ({fixture_ref!r}) -- "
            "DATA_DIR must point CI at the committed deterministic fixture"
        )

    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if re.search(r'DATA_DIR\s*[:=]\s*"?data"?\s*$', stripped):
            raise FixtureError(
                f"{ci_path}:{line_no}: sets DATA_DIR to the plain 'data' directory -- CI must load "
                f"the canonical fixture ({fixture_ref}) instead: {stripped!r}"
            )

    if re.search(r"if present", text, re.IGNORECASE):
        raise FixtureError(
            f"{ci_path}: still contains 'if present' language for what must now be a required, "
            "validated fixture load (a zero-file load must fail, not silently succeed)"
        )

    return [
        f"CI workflow references the canonical fixture directory ({fixture_ref}).",
        "CI workflow contains no 'if present' language and no bare DATA_DIR=data.",
    ]


def validate(fixture_root: Path, repo_root: Path) -> list[str]:
    diagnostics: list[str] = []

    if not fixture_root.is_dir():
        raise FixtureError(f"fixture directory not found: {fixture_root}")

    # Only *.csv files are subject to the "exactly these 15, no more, no
    # fewer" check -- non-CSV files (e.g. this directory's own README.md
    # explaining the fixture) are allowed alongside the managed CSVs.
    actual_filenames = {p.name for p in fixture_root.iterdir() if p.is_file()}
    actual_csv_filenames = {n for n in actual_filenames if n.endswith(".csv")}
    missing = EXPECTED_FILENAMES - actual_csv_filenames
    if missing:
        raise FixtureError(f"fixture is missing managed table CSV(s): {sorted(missing)}")
    extra = actual_csv_filenames - EXPECTED_FILENAMES
    if extra:
        raise FixtureError(f"fixture contains unexpected CSV file(s) not in MANAGED_TABLES: {sorted(extra)}")
    diagnostics.append(f"Exactly the {len(EXPECTED_FILENAMES)} expected managed-table CSV(s) are present.")

    tables: dict[str, tuple[list[str], list[list[str]]]] = {}
    for table in MANAGED_TABLES:
        header, rows = _read_rows(fixture_root / f"{table}.csv")
        tables[table] = (header, rows)

        expected_count = EXPECTED_ROW_COUNTS[table]
        if len(rows) != expected_count:
            raise FixtureError(
                f"{table}.csv: has {len(rows)} data row(s), expected exactly {expected_count}"
            )
    diagnostics.append("Every file: valid UTF-8, LF-only line endings, trailing newline, unique nonempty headers.")
    diagnostics.append("Every managed table has exactly its expected deterministic row count.")

    # --- secret-shaped / SSN-shaped value scan ------------------------------
    for table, (header, rows) in tables.items():
        for row in rows:
            for col, val in zip(header, row):
                if not val:
                    continue
                if _SECRET_LOOKALIKE_RE.search(val):
                    raise FixtureError(f"{table}.csv: column {col!r} looks secret-shaped: {val!r}")
                if _SSN_SHAPED_RE.match(val):
                    raise FixtureError(f"{table}.csv: column {col!r} looks like a real SSN: {val!r}")
    diagnostics.append("No secret-shaped or SSN-shaped values found in any fixture file.")

    # --- no dynamically generated date/time values --------------------------
    now = datetime.now(timezone.utc)
    today_variants = {now.strftime("%Y-%m-%d"), now.strftime("%Y%m%d")}
    for table, (header, rows) in tables.items():
        for row in rows:
            for col, val in zip(header, row):
                for m in _DATE_SHAPED_RE.finditer(val):
                    if m.group(0) in today_variants:
                        raise FixtureError(
                            f"{table}.csv: column {col!r} value {val!r} equals this test run's "
                            "current date -- fixture dates must be fixed/synthetic, never "
                            "generated from the current date/time"
                        )
    diagnostics.append("No fixture value matches this test run's current date (dates are fixed, not generated).")

    # --- fixed hub identifiers present --------------------------------------
    for table, (id_col, expected_id) in FIXED_HUB_IDS.items():
        header, rows = tables[table]
        idx = header.index(id_col)
        actual_ids = {row[idx] for row in rows}
        if expected_id not in actual_ids:
            raise FixtureError(f"{table}.csv: expected fixed id {expected_id!r} not found in column {id_col!r}")
    diagnostics.append("Fixed hub identifiers (person-1/practitioner-1/location-1/encounter-1) are present.")

    # --- cross-file foreign key relationships -------------------------------
    parent_pk_values: dict[tuple[str, str], set[str]] = {}
    for child_table, child_col, parent_table, parent_col in KNOWN_FOREIGN_KEYS:
        key = (parent_table, parent_col)
        if key not in parent_pk_values:
            header, rows = tables[parent_table]
            idx = header.index(parent_col)
            parent_pk_values[key] = {row[idx] for row in rows}

        header, rows = tables[child_table]
        idx = header.index(child_col)
        for row in rows:
            val = row[idx]
            if val and val not in parent_pk_values[key]:
                raise FixtureError(
                    f"{child_table}.csv: {child_col}={val!r} does not exist in "
                    f"{parent_table}.csv's {parent_col} column -- broken fixture relationship"
                )
    diagnostics.append(f"All {len(KNOWN_FOREIGN_KEYS)} known cross-file foreign-key relationships resolve.")

    _validate_gitignore(fixture_root, repo_root)
    diagnostics.append("Fixture directory is not excluded by .gitignore.")

    diagnostics.extend(_validate_ci_workflow(repo_root))

    return diagnostics


# ---------------------------------------------------------------------------
# Negative controls: prove `validate()` actually rejects a broken fixture,
# not just that it accepts the real one. Every scenario mutates a private
# temporary copy of the real, committed fixture -- the committed files
# under tests/fixtures/ci/complete_snapshot/ are never written to.
# ---------------------------------------------------------------------------


def _copy_real_fixture(repo_root: Path) -> Path:
    real_fixture = repo_root / FIXTURE_RELATIVE_DIR
    tmp_dir = Path(tempfile.mkdtemp(prefix="tuva-ci-fixture-negctl-"))
    scratch = tmp_dir / "complete_snapshot"
    shutil.copytree(real_fixture, scratch)
    return scratch


def _expect_failure(repo_root: Path, mutate, description: str) -> str:
    scratch = _copy_real_fixture(repo_root)
    try:
        mutate(scratch)
        validate(scratch, repo_root)
    except FixtureError:
        return f"OK: {description} is correctly rejected."
    else:
        raise FixtureError(f"NEGATIVE CONTROL FAILED: {description} was NOT rejected by validate()")
    finally:
        shutil.rmtree(scratch.parent, ignore_errors=True)


def run_negative_controls(repo_root: Path) -> list[str]:
    results = []

    def remove_one_csv(scratch: Path) -> None:
        (scratch / "practitioner.csv").unlink()

    results.append(_expect_failure(repo_root, remove_one_csv, "a fixture missing one managed CSV"))

    def add_unexpected_csv(scratch: Path) -> None:
        (scratch / "not_a_managed_table.csv").write_text("id\nx\n", encoding="utf-8")

    results.append(_expect_failure(repo_root, add_unexpected_csv, "a fixture with an unexpected extra CSV"))

    def truncate_a_row(scratch: Path) -> None:
        path = scratch / "patient.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        # Drop the last field from the data row -- too few fields for the header.
        lines[1] = ",".join(lines[1].split(",")[:-1])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    results.append(_expect_failure(repo_root, truncate_a_row, "a row with too few fields"))

    def duplicate_header(scratch: Path) -> None:
        path = scratch / "location.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        header_fields = lines[0].split(",")
        header_fields[-1] = header_fields[0]  # duplicate the first column name
        lines[0] = ",".join(header_fields)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    results.append(_expect_failure(repo_root, duplicate_header, "a duplicate column header"))

    def break_relationship(scratch: Path) -> None:
        path = scratch / "encounter.csv"
        header, rows = _read_rows(path)
        idx = header.index("person_id")
        rows[0][idx] = "person-does-not-exist"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

    results.append(_expect_failure(repo_root, break_relationship, "a broken cross-file foreign-key relationship"))

    return results


def main(argv: list[str]) -> int:
    if len(argv) > 3:
        print("usage: test_ci_fixture.py [fixture_root] [repo_root]", file=sys.stderr)
        return 2

    repo_root = Path(argv[2]) if len(argv) >= 3 else REPO_ROOT
    fixture_root = Path(argv[1]) if len(argv) >= 2 else repo_root / FIXTURE_RELATIVE_DIR

    try:
        diagnostics = validate(fixture_root, repo_root)
    except FixtureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    # Negative controls only make sense against the real, committed
    # fixture (they copy it and mutate the copy) -- skip them when a
    # scratch fixture_root/repo_root was explicitly provided, so this
    # script itself stays usable as a generic validator for other tests.
    negctl_results: list[str] = []
    if len(argv) < 2:
        try:
            negctl_results = run_negative_controls(repo_root)
        except FixtureError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

    print(
        f"PASS: {fixture_root} is a complete, structurally valid, internally consistent "
        "deterministic CI fixture covering every managed table."
    )
    for d in diagnostics:
        print(f"  - {d}")
    for r in negctl_results:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
