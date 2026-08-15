#!/usr/bin/env bash
# Regression test for scripts/run_tests.sh
#
# Verifies (by behavior, not by grepping the script source) that
# run_tests.sh:
#   - resolves the canonical db/tests/ SQL-validation directory from its
#     own script location (SCRIPT_DIR/REPO_ROOT), not the caller's
#     current working directory -- so it behaves identically whether
#     invoked as `bash scripts/run_tests.sh` from the repo root or via an
#     absolute path from anywhere else;
#   - applies db/tests/zz_results.sql exactly once, as test-harness setup,
#     and never treats it as a validation case (no --csv invocation, no
#     result CSV of its own);
#   - executes every other db/tests/*.sql fixture exactly once, in
#     deterministic lexical order;
#   - passes the configured schema/terminology_schema variables to every
#     validation-case invocation;
#   - never references the retired db/tables/tests/ path.
#
# This test would FAIL against the old, retired "db/tables/tests" path
# (that directory no longer exists) and would also FAIL if zz_results.sql
# regressed into being treated as an ordinary test case, or if run_tests.sh
# silently depended on being invoked from the repository root.
#
# No real PostgreSQL server is required: stub `psql` and `python3`
# executables are put first on PATH.
#   - psql just records the arguments it was called with and exits 0.
#   - python3 stands in for `python scripts/ingest_test_csv.py ...` and
#     emits a tiny, already-valid normalized CSV so run_tests.sh can move
#     on to its \copy/summary steps without needing real psql --csv output.
#
# A small, representative scratch fixture set (three fake validation
# cases plus zz_results.sql) is used instead of copying the repository's
# full SQL test suite, so exact invocation order and counts are easy to
# assert.
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
INVOKE_FROM_DIR="$TMP_DIR/elsewhere"
mkdir -p "$STUB_BIN_DIR" "$INVOKE_FROM_DIR"
: > "$PSQL_LOG"

# --- Build a scratch repo root with just what run_tests.sh needs --------
# (its own scripts/ + a small, representative db/tests/ fixture set), so
# the script's own-location-relative paths resolve consistently and any
# files it writes (tmp/test_results/*) live entirely under $TMP_DIR.
mkdir -p "$SCRATCH_REPO/scripts" "$SCRATCH_REPO/db/tests"
cp "$REPO_ROOT/scripts/run_tests.sh" "$SCRATCH_REPO/scripts/run_tests.sh"
cp "$REPO_ROOT/scripts/ingest_test_csv.py" "$SCRATCH_REPO/scripts/ingest_test_csv.py"

# Setup file (harness, not a validation case).
cat > "$SCRATCH_REPO/db/tests/zz_results.sql" <<'SQL'
-- Minimal stand-in for the real zz_results.sql results-table setup.
SELECT 1;
SQL

# Three representative validation-case fixtures, named so their sorted
# execution order is unambiguous and easy to assert.
cat > "$SCRATCH_REPO/db/tests/a_first_smoke.sql" <<'SQL'
SELECT 'a_first_smoke' AS test, true AS pass;
SQL
cat > "$SCRATCH_REPO/db/tests/b_second_smoke.sql" <<'SQL'
SELECT 'b_second_smoke' AS test, true AS pass;
SQL
cat > "$SCRATCH_REPO/db/tests/c_third_addon.sql" <<'SQL'
SELECT 'c_third_addon' AS test, true AS pass;
SQL

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
# Stands in for `python3 .../ingest_test_csv.py RUN_ID file1.csv ...`.
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

# --- Run run_tests.sh via an ABSOLUTE path from a directory that is
#     neither the scratch repo root nor the real repository root -------
# Proves path resolution comes from the script's own location
# (SCRIPT_DIR/REPO_ROOT inside run_tests.sh), not the caller's cwd. If
# run_tests.sh regressed to relative "db/tests/..." paths, this would
# fail: "elsewhere" has no db/ directory at all.
EXPECTED_SCHEMA="regression_test_schema"
EXPECTED_TERM_SCHEMA="regression_test_term_schema"
EXPECTED_RUN_ID="regression_test_run"

set +e
(
  cd "$INVOKE_FROM_DIR" && \
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$EXPECTED_SCHEMA" \
    TERMINOLOGY_SCHEMA="$EXPECTED_TERM_SCHEMA" \
    RUN_ID="$EXPECTED_RUN_ID" \
    PSQL_LOG="$PSQL_LOG" \
    bash "$SCRATCH_REPO/scripts/run_tests.sh"
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

SETUP_SQL="$SCRATCH_REPO/db/tests/zz_results.sql"
FIRST_SQL="$SCRATCH_REPO/db/tests/a_first_smoke.sql"
SECOND_SQL="$SCRATCH_REPO/db/tests/b_second_smoke.sql"
THIRD_SQL="$SCRATCH_REPO/db/tests/c_third_addon.sql"

# --- Assertions ------------------------------------------------------------

# 1) The script must exit successfully, invoked by absolute path from a
#    directory with no db/ subtree of its own -- proving cwd-independence.
if [[ $RUN_STATUS -ne 0 ]]; then
  fail "scripts/run_tests.sh exited with status $RUN_STATUS when invoked from a non-repo-root directory"
fi

# 2) The results setup file comes from db/tests/zz_results.sql (resolved
#    against the script's own location, inside the scratch repo).
if ! grep -qF -- "-f $SETUP_SQL" "$PSQL_LOG"; then
  fail "no psql invocation loaded the setup file $SETUP_SQL"
fi

# 3) zz_results.sql is applied exactly once.
SETUP_COUNT="$(grep -cF -- "$SETUP_SQL" "$PSQL_LOG")"
if [[ "$SETUP_COUNT" -ne 1 ]]; then
  fail "expected zz_results.sql to be referenced exactly once, found $SETUP_COUNT"
fi

# 4) zz_results.sql is not executed as a normal CSV-producing test case
#    (its one and only invocation must be the plain setup call -- no
#    --csv flag, no terminology_schema variable).
SETUP_LINE="$(grep -F -- "$SETUP_SQL" "$PSQL_LOG")"
if grep -qF -- "--csv" <<<"$SETUP_LINE"; then
  fail "zz_results.sql was invoked with --csv -- it must run as setup only, not a validation case" "$SETUP_LINE"
fi
if grep -qF -- "terminology_schema" <<<"$SETUP_LINE"; then
  fail "zz_results.sql's invocation carried terminology_schema -- that variable is only for validation cases" "$SETUP_LINE"
fi

# 5) Every other fixture SQL test is invoked exactly once each.
for f in "$FIRST_SQL" "$SECOND_SQL" "$THIRD_SQL"; do
  count="$(grep -cF -- "-f $f" "$PSQL_LOG")"
  if [[ "$count" -ne 1 ]]; then
    fail "expected $f to be invoked exactly once, found $count"
  fi
done

# 6) Test cases run in deterministic lexical order: a_first, then
#    b_second, then c_third.
FIRST_LINE_NO="$(grep -nF -- "-f $FIRST_SQL" "$PSQL_LOG" | cut -d: -f1)"
SECOND_LINE_NO="$(grep -nF -- "-f $SECOND_SQL" "$PSQL_LOG" | cut -d: -f1)"
THIRD_LINE_NO="$(grep -nF -- "-f $THIRD_SQL" "$PSQL_LOG" | cut -d: -f1)"
if ! [[ "$FIRST_LINE_NO" -lt "$SECOND_LINE_NO" && "$SECOND_LINE_NO" -lt "$THIRD_LINE_NO" ]]; then
  fail "SQL test fixtures did not run in deterministic lexical order (a_first=$FIRST_LINE_NO, b_second=$SECOND_LINE_NO, c_third=$THIRD_LINE_NO)"
fi

# 7) Each validation-case invocation carries the configured schema and
#    terminology_schema variables.
for f in "$FIRST_SQL" "$SECOND_SQL" "$THIRD_SQL"; do
  line="$(grep -F -- "-f $f" "$PSQL_LOG")"
  if ! grep -qF -- "-v schema=${EXPECTED_SCHEMA}" <<<"$line"; then
    fail "invocation for $f did not include -v schema=${EXPECTED_SCHEMA}" "$line"
  fi
  if ! grep -qF -- "-v terminology_schema=${EXPECTED_TERM_SCHEMA}" <<<"$line"; then
    fail "invocation for $f did not include -v terminology_schema=${EXPECTED_TERM_SCHEMA}" "$line"
  fi
done

# 8) No invocation may reference the obsolete db/tables/tests/ path.
if grep -qF -- "db/tables/tests/" "$PSQL_LOG"; then
  fail "found a psql invocation referencing the obsolete db/tables/tests/ path"
fi

# 9) Generated results stay under the scratch repo's own tmp/test_results
#    (cleanable as part of this test's $TMP_DIR teardown) -- not the real
#    repository's tmp/ directory, and not the invoking "elsewhere" cwd.
if [[ ! -d "$SCRATCH_REPO/tmp/test_results" ]]; then
  fail "expected generated results under $SCRATCH_REPO/tmp/test_results, directory not found"
fi
if [[ -d "$INVOKE_FROM_DIR/tmp" ]]; then
  fail "run_tests.sh wrote output relative to the caller's cwd ($INVOKE_FROM_DIR) instead of its own repo root"
fi
if compgen -G "$REPO_ROOT/tmp/test_results/regression_test_run*" > /dev/null; then
  fail "run_tests.sh wrote output into the real repository's tmp/ directory instead of the scratch repo's"
fi

echo "PASS: run_tests.sh resolves db/tests/ from its own script location (works"
echo "      when invoked by absolute path from a non-repo-root cwd), applies"
echo "      zz_results.sql exactly once as setup (never as a --csv validation"
echo "      case), runs every other db/tests/*.sql fixture exactly once in"
echo "      deterministic lexical order with -v schema=${EXPECTED_SCHEMA} and"
echo "      -v terminology_schema=${EXPECTED_TERM_SCHEMA}, never references the"
echo "      obsolete db/tables/tests/ path, and writes results only under its"
echo "      own repo root's tmp/test_results/."
