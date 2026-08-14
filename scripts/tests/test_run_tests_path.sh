#!/usr/bin/env bash
# Regression test for scripts/run_tests.sh
#
# Verifies (by behavior, not by grepping the script source) that
# run_tests.sh loads its results-table setup and SQL test files from
# db/tables/tests. This test would FAIL against the old, buggy "db/tests"
# path: that directory does not exist, so the setup psql call would target
# a missing file and the *.sql discovery glob would never see a file under
# db/tables/tests.
#
# No real PostgreSQL server is required: stub `psql` and `python3`
# executables are put first on PATH.
#   - psql just records the arguments it was called with and exits 0.
#   - python3 stands in for `python scripts/ingest_test_csv.py ...` and
#     emits a tiny, already-valid normalized CSV so run_tests.sh can move
#     on to its \copy/summary steps without needing real psql --csv output.
#
# run_tests.sh writes to ./tmp/test_results (relative to cwd) and expects
# to be run from a repository root. To keep this test hermetic and fully
# cleanable, we run it against a scratch copy of the repo's db/ and
# scripts/ directories rather than the real working tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

STUB_BIN_DIR="$TMP_DIR/bin"
PSQL_LOG="$TMP_DIR/psql_invocations.log"
SCRATCH_REPO="$TMP_DIR/repo"
mkdir -p "$STUB_BIN_DIR"
: > "$PSQL_LOG"

# --- Build a scratch repo root with just what run_tests.sh needs --------
# (its own db/tables/tests/*.sql fixtures + the scripts it shells out to),
# so the script's repository-relative paths resolve consistently and any
# files it writes (tmp/test_results/*) live entirely under $TMP_DIR.
mkdir -p "$SCRATCH_REPO/scripts"
mkdir -p "$SCRATCH_REPO/db/tables/tests"
cp "$REPO_ROOT/scripts/run_tests.sh" "$SCRATCH_REPO/scripts/run_tests.sh"
cp "$REPO_ROOT/scripts/ingest_test_csv.py" "$SCRATCH_REPO/scripts/ingest_test_csv.py"
cp "$REPO_ROOT"/db/tables/tests/*.sql "$SCRATCH_REPO/db/tables/tests/"

if [[ ! -f "$SCRATCH_REPO/db/tables/tests/zz_results.sql" ]]; then
  echo "FAIL: fixture setup problem -- db/tables/tests/zz_results.sql not found in the real repo." >&2
  exit 1
fi

# --- Build the psql stub -------------------------------------------------
# Records every invocation (as a single space-joined line) to PSQL_LOG and
# exits 0 without touching a real database.
cat > "$STUB_BIN_DIR/psql" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PSQL_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/psql"

# --- Build the python3 stub ----------------------------------------------
# Stands in for `python3 scripts/ingest_test_csv.py RUN_ID file1.csv ...`.
# Ignores its arguments and prints the minimum valid normalized CSV
# (header + one row) run_tests.sh needs to proceed to its \copy step.
cat > "$STUB_BIN_DIR/python3" <<'STUB'
#!/usr/bin/env bash
run_id="${2:-stub_run}"
printf 'run_id,suite,test,pass,payload\n'
printf '%s,stub_suite,stub_test,true,{}\n' "$run_id"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/python3"

# --- Run run_tests.sh with the stubs on PATH, from the scratch repo -----
EXPECTED_SCHEMA="regression_test_schema"
EXPECTED_TERM_SCHEMA="regression_test_term_schema"
EXPECTED_RUN_ID="regression_test_run"

set +e
(
  cd "$SCRATCH_REPO" && \
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$EXPECTED_SCHEMA" \
    TERMINOLOGY_SCHEMA="$EXPECTED_TERM_SCHEMA" \
    RUN_ID="$EXPECTED_RUN_ID" \
    PSQL_LOG="$PSQL_LOG" \
    bash scripts/run_tests.sh
) > "$TMP_DIR/run_tests.stdout" 2>&1
RUN_STATUS=$?
set -e

fail() {
  echo "FAIL: $1" >&2
  echo "----- run_tests.sh output -----" >&2
  cat "$TMP_DIR/run_tests.stdout" >&2
  echo "----- recorded psql invocations -----" >&2
  cat "$PSQL_LOG" >&2
  echo "--------------------------------------" >&2
  exit 1
}

# --- Assertions ------------------------------------------------------------

# 1) The script must exit successfully with the stubs in place.
if [[ $RUN_STATUS -ne 0 ]]; then
  fail "scripts/run_tests.sh exited with status $RUN_STATUS"
fi

# 2) The results-table setup must load db/tables/tests/zz_results.sql.
if ! grep -qF -- "-f db/tables/tests/zz_results.sql" "$PSQL_LOG"; then
  fail "no psql invocation loaded db/tables/tests/zz_results.sql"
fi

# 3) At least one SQL test file under db/tables/tests/ must be applied.
TEST_FILE_INVOCATIONS="$(grep -F -- "-f db/tables/tests/" "$PSQL_LOG" || true)"
if [[ -z "$TEST_FILE_INVOCATIONS" ]]; then
  fail "no psql invocation applied a SQL file under db/tables/tests/"
fi

# 4) Those invocations must carry the configured schema variable.
if ! grep -qF -- "-v schema=${EXPECTED_SCHEMA}" <<<"$TEST_FILE_INVOCATIONS"; then
  fail "psql invocations for db/tables/tests did not include -v schema=${EXPECTED_SCHEMA}"
fi

# 5) Those invocations must carry the configured terminology schema variable.
if ! grep -qF -- "-v terminology_schema=${EXPECTED_TERM_SCHEMA}" <<<"$TEST_FILE_INVOCATIONS"; then
  fail "psql invocations for db/tables/tests did not include -v terminology_schema=${EXPECTED_TERM_SCHEMA}"
fi

# 6) No invocation may reference the obsolete db/tests/ path.
if grep -qF -- "db/tests/" "$PSQL_LOG"; then
  fail "found a psql invocation referencing the obsolete db/tests/ path"
fi

echo "PASS: run_tests.sh loads db/tables/tests/zz_results.sql and applies SQL"
echo "      test files from db/tables/tests/ with -v schema=${EXPECTED_SCHEMA}"
echo "      and -v terminology_schema=${EXPECTED_TERM_SCHEMA}."
