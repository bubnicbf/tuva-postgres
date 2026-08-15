#!/usr/bin/env bash
# Structural + behavioral regression test for the canonical SQL
# validation-test location.
#
# db/tests/ is the single canonical home for SQL data-quality/validation
# queries and their test-harness setup (zz_results.sql). This is a
# separate concern from:
#   - deployable DDL, which lives exclusively under versioned migration
#     directories in db/migrations/sql/ (see
#     scripts/tests/test_versioned_migration_layout.sh, which owns
#     migration-organization assertions -- this test owns SQL-validation-
#     organization assertions instead, to keep responsibilities distinct)
#   - shell/Python structural regression tests under scripts/tests/
#   - Python unit/integration tests under tests/unit/ and tests/integration/
#
# Part 1 (static) inspects the real, committed repository structure and
# manifests. Part 2 (behavioral) copies the real db/tests/ fixture set
# into a scratch repo and runs the real scripts/run_tests.sh against it
# with stubbed psql/python3, proving -- over the FULL real inventory, not
# just a representative sample -- that zz_results.sql runs exactly once as
# setup and every other file runs exactly once, in deterministic order.
# (scripts/tests/test_run_tests_path.sh separately proves the same
# behavior with a small synthetic fixture set focused on cwd-independence
# and variable-passing; this test instead exercises the real, full db/tests/
# inventory so a genuinely orphaned or duplicated real fixture is caught.)
#
# Database-free and network-free throughout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# =============================================================================
# Part 1: static structural checks against the real, committed repository
# =============================================================================

STATIC_TMP="$(mktemp)"
cleanup_static() { rm -f "$STATIC_TMP"; }
trap cleanup_static EXIT

set +e
python3 - "$REPO_ROOT" > "$STATIC_TMP" 2>&1 <<'PY'
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


db_tests_dir = (repo_root / "db" / "tests").resolve()
migrations_dir = (repo_root / "db" / "migrations").resolve()
sql_root = (migrations_dir / "sql").resolve()

# --- db/tests/ exists -------------------------------------------------------
check(db_tests_dir.is_dir(), f"canonical SQL-test directory not found: {db_tests_dir}")

# --- db/tests/zz_results.sql exists -----------------------------------------
results_setup = db_tests_dir / "zz_results.sql"
check(results_setup.is_file(), f"expected test-harness setup file not found: {results_setup}")

# --- at least one actual SQL test case exists besides zz_results.sql -------
if db_tests_dir.is_dir():
    all_db_tests_sql = sorted(db_tests_dir.glob("*.sql"))
else:
    all_db_tests_sql = []
validation_cases = [f for f in all_db_tests_sql if f.name != "zz_results.sql"]
check(len(validation_cases) >= 1, "no SQL validation case files found in db/tests/ besides zz_results.sql")

# --- obsolete db/tables/tests/ does not exist -------------------------------
legacy_tables_tests = repo_root / "db" / "tables" / "tests"
check(not legacy_tables_tests.exists(), f"obsolete directory still exists: {legacy_tables_tests}")

# --- obsolete db/tests.sql (singular file, not the db/tests/ directory) ----
legacy_singular = repo_root / "db" / "tests.sql"
check(not legacy_singular.exists(), f"obsolete file still exists: {legacy_singular}")

# --- no committed SQL validation file lives in another known test-like
#     location ---------------------------------------------------------------
other_known_locations = [
    repo_root / "tests" / "sql",
    repo_root / "sql" / "tests",
    repo_root / "scripts" / "tests" / "sql",
]
for loc in other_known_locations:
    if loc.exists():
        stray = sorted(loc.rglob("*.sql"))
        check(not stray, f"SQL file(s) found in a non-canonical test-like location {loc}: {stray}")

# --- migration manifests do not reference anything under db/tests/ ---------
manifest_paths = sorted(migrations_dir.glob("*.json")) if migrations_dir.is_dir() else []
check(len(manifest_paths) >= 1, f"no migration manifests found under {migrations_dir}")
for manifest_path in manifest_paths:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel in raw.get("files", []):
        resolved = (repo_root / rel).resolve()
        check(
            not resolved.is_relative_to(db_tests_dir),
            f"{manifest_path.name} references a file under db/tests/ (validation SQL, not DDL): {rel}",
        )

# --- SQL files under db/tests/ are not located under migration directories -
for f in all_db_tests_sql:
    check(
        not f.resolve().is_relative_to(sql_root),
        f"db/tests/ file is nested under a migration directory: {f}",
    )
# ...and the reverse: no migration SQL file is nested under db/tests/.
if sql_root.is_dir():
    for f in sorted(sql_root.rglob("*.sql")):
        check(
            not f.resolve().is_relative_to(db_tests_dir),
            f"migration SQL file is nested under db/tests/: {f}",
        )

# --- scripts/run_tests.sh resolves the canonical db/tests/ location, and
#     does not reference the obsolete db/tables/tests/ path -----------------
run_tests_sh = (repo_root / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
check(
    'SQL_TEST_DIR="$REPO_ROOT/db/tests"' in run_tests_sh,
    "scripts/run_tests.sh does not define the canonical SQL_TEST_DIR from its own REPO_ROOT",
)
check(
    "db/tables/tests" not in run_tests_sh,
    "scripts/run_tests.sh still references the obsolete db/tables/tests/ path",
)
# The runner must resolve its own location rather than depending on the
# caller's current working directory.
check(
    'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in run_tests_sh,
    "scripts/run_tests.sh does not derive its own location via BASH_SOURCE "
    "(required for cwd-independent path resolution)",
)

# --- zz_results.sql is applied once as setup and excluded from the
#     validation-case discovery loop (static corroboration of the
#     behavioral proof in Part 2 below) --------------------------------------
check(
    "RESULTS_SETUP_SQL" in run_tests_sh,
    "scripts/run_tests.sh does not define a distinct RESULTS_SETUP_SQL variable for zz_results.sql",
)
check(
    '[[ "$f" == "$RESULTS_SETUP_SQL" ]] && continue' in run_tests_sh,
    "scripts/run_tests.sh does not exclude RESULTS_SETUP_SQL (zz_results.sql) from its "
    "validation-case discovery loop",
)

# --- validation files have unique basenames ---------------------------------
basenames = [f.name for f in all_db_tests_sql]
duplicates = sorted({b for b in basenames if basenames.count(b) > 1})
check(not duplicates, f"duplicate SQL validation file basenames found in db/tests/: {duplicates}")

if failures:
    print("FAIL: SQL-test layout violations found:\n")
    for f in failures:
        print(f"  - {f}")
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)

print(
    f"PASS (static): db/tests/ is the canonical SQL-validation location "
    f"({len(validation_cases)} validation case(s) + zz_results.sql setup), "
    "no obsolete db/tables/tests/ or db/tests.sql, no stray SQL validation "
    "files elsewhere, no migration manifest references db/tests/, no "
    "cross-nesting between db/tests/ and db/migrations/sql/, "
    "scripts/run_tests.sh resolves db/tests/ from its own location and "
    "excludes zz_results.sql from validation-case discovery, and all "
    "validation-file basenames are unique."
)
PY
STATIC_STATUS=$?
set -e

cat "$STATIC_TMP"
if [[ $STATIC_STATUS -ne 0 ]]; then
  exit 1
fi

# =============================================================================
# Part 2: behavioral check against the FULL real db/tests/ fixture set
# =============================================================================

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

STUB_BIN_DIR="$TMP_DIR/bin"
PSQL_LOG="$TMP_DIR/psql_invocations.log"
SCRATCH_REPO="$TMP_DIR/repo"
mkdir -p "$STUB_BIN_DIR" "$SCRATCH_REPO/scripts" "$SCRATCH_REPO/db/tests"
: > "$PSQL_LOG"

cp "$REPO_ROOT/scripts/run_tests.sh" "$SCRATCH_REPO/scripts/run_tests.sh"
cp "$REPO_ROOT/scripts/ingest_test_csv.py" "$SCRATCH_REPO/scripts/ingest_test_csv.py"
cp "$REPO_ROOT"/db/tests/*.sql "$SCRATCH_REPO/db/tests/"

cat > "$STUB_BIN_DIR/psql" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PSQL_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/psql"

cat > "$STUB_BIN_DIR/python3" <<'STUB'
#!/usr/bin/env bash
run_id="${2:-stub_run}"
printf 'run_id,suite,test,pass,payload\n'
printf '%s,stub_suite,stub_test,true,{}\n' "$run_id"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/python3"

set +e
(
  cd "$SCRATCH_REPO" && \
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="regression_test_schema" \
    TERMINOLOGY_SCHEMA="regression_test_term_schema" \
    RUN_ID="regression_test_run" \
    PSQL_LOG="$PSQL_LOG" \
    bash scripts/run_tests.sh
) > "$TMP_DIR/run_tests.stdout" 2>&1
RUN_STATUS=$?
set -e

fail() {
  echo "FAIL: $1" >&2
  echo "----- run_tests.sh output -----" >&2
  cat "$TMP_DIR/run_tests.stdout" >&2
  exit 1
}

if [[ $RUN_STATUS -ne 0 ]]; then
  fail "scripts/run_tests.sh exited with status $RUN_STATUS against the full real db/tests/ fixture set"
fi

# Real, full inventory: every *.sql under the real db/tests/, sorted.
mapfile -t REAL_SQL_FILES < <(cd "$REPO_ROOT" && ls -1 db/tests/*.sql | sort)
REAL_VALIDATION_COUNT=0
for f in "${REAL_SQL_FILES[@]}"; do
  base="$(basename "$f")"
  [[ "$base" == "zz_results.sql" ]] && continue
  REAL_VALIDATION_COUNT=$((REAL_VALIDATION_COUNT + 1))
done

# zz_results.sql: applied exactly once, total (setup call only).
SETUP_PATH="$SCRATCH_REPO/db/tests/zz_results.sql"
SETUP_COUNT="$(grep -cF -- "$SETUP_PATH" "$PSQL_LOG")"
if [[ "$SETUP_COUNT" -ne 1 ]]; then
  fail "expected zz_results.sql to be referenced exactly once across the full real fixture run, found $SETUP_COUNT"
fi

# Every other real validation file invoked exactly once each.
MISSING=""
DUPLICATED=""
for f in "${REAL_SQL_FILES[@]}"; do
  base="$(basename "$f")"
  [[ "$base" == "zz_results.sql" ]] && continue
  count="$(grep -cF -- "$SCRATCH_REPO/db/tests/$base" "$PSQL_LOG")"
  if [[ "$count" -eq 0 ]]; then
    MISSING+=" $base"
  elif [[ "$count" -gt 1 ]]; then
    DUPLICATED+=" $base($count)"
  fi
done
if [[ -n "$MISSING" ]]; then
  fail "real db/tests/ validation file(s) never invoked:$MISSING"
fi
if [[ -n "$DUPLICATED" ]]; then
  fail "real db/tests/ validation file(s) invoked more than once:$DUPLICATED"
fi

# Deterministic order: the order validation-case files first appear in the
# psql log must match the sorted real filename order. Built by scanning
# the log for each known basename's first matching line number.
declare -A FIRST_LINE=()
for f in "${REAL_SQL_FILES[@]}"; do
  base="$(basename "$f")"
  [[ "$base" == "zz_results.sql" ]] && continue
  line_no="$(grep -nF -- "$SCRATCH_REPO/db/tests/$base" "$PSQL_LOG" | head -1 | cut -d: -f1)"
  FIRST_LINE["$base"]="$line_no"
done
mapfile -t EXPECTED_ORDER < <(
  for f in "${REAL_SQL_FILES[@]}"; do
    base="$(basename "$f")"
    [[ "$base" == "zz_results.sql" ]] && continue
    printf '%s\n' "$base"
  done
)
PREV_LINE=0
ORDER_OK=1
for base in "${EXPECTED_ORDER[@]}"; do
  ln="${FIRST_LINE[$base]}"
  if [[ "$ln" -le "$PREV_LINE" ]]; then
    ORDER_OK=0
    break
  fi
  PREV_LINE="$ln"
done
if [[ "$ORDER_OK" -ne 1 ]]; then
  fail "real db/tests/ validation files did not execute in deterministic sorted-filename order"
fi

echo "PASS (behavioral): scripts/run_tests.sh, run against the full real"
echo "                   db/tests/ fixture set ($REAL_VALIDATION_COUNT validation"
echo "                   case(s) + zz_results.sql), applies zz_results.sql exactly"
echo "                   once as setup, executes every other file exactly once,"
echo "                   and runs them in deterministic sorted-filename order."
