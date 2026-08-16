#!/usr/bin/env bash
# Regression test for scripts/run_tests.sh
#
# Guards against GitHub Actions workflow YAML being embedded at the end of
# scripts/run_tests.sh. This previously happened: a trailing
#   on:
#     schedule:
#       - cron: "0 6 * * *"
#     push:
#     pull_request:
# block was accidentally appended to the script. That block is YAML, not
# Bash. Critically, `on:` is a syntactically valid (if nonsensical) Bash
# command name, so `bash -n scripts/run_tests.sh` alone does NOT catch this
# -- the script still parses. Only actually running it and observing that
# Bash tries (and fails) to execute `on:`, `schedule:`, `push:`, and
# `pull_request:` as commands reveals the failure.
#
# This test has two parts:
#   1. A behavioral check: run run_tests.sh end-to-end against a minimal,
#      hermetic scratch repo (stubbed psql/python3, no real Postgres, no
#      network) and assert it (a) exits 0 and (b) produces no
#      "<keyword>: command not found" output for any workflow trigger
#      keyword.
#   2. A narrow static guard on the real script source: assert it contains
#      no bare top-level YAML trigger-declaration lines
#      (on:/schedule:/- cron:/push:/pull_request:). This supplements, but
#      does not replace, the behavioral check above -- see part 1 for why
#      a static check alone would be insufficient.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REAL_RUN_TESTS="$REPO_ROOT/scripts/run_tests.sh"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

STUB_BIN_DIR="$TMP_DIR/bin"
SCRATCH_REPO="$TMP_DIR/repo"
PSQL_LOG="$TMP_DIR/psql_invocations.log"
mkdir -p "$STUB_BIN_DIR" "$SCRATCH_REPO/scripts/lib" "$SCRATCH_REPO/db/tests"
: > "$PSQL_LOG"

# --- Minimal scratch fixtures --------------------------------------------
# Only two tiny SQL fixtures are needed -- no need to duplicate the
# repository's full SQL test suite for this check.
cp "$REAL_RUN_TESTS" "$SCRATCH_REPO/scripts/run_tests.sh"
cp "$REPO_ROOT/scripts/ingest_test_csv.py" "$SCRATCH_REPO/scripts/ingest_test_csv.py"
cp "$REPO_ROOT/scripts/lib/postgres_identifiers.sh" "$SCRATCH_REPO/scripts/lib/postgres_identifiers.sh"

cat > "$SCRATCH_REPO/db/tests/zz_results.sql" <<'SQL'
-- Minimal stand-in for the real zz_results.sql results-table setup.
SELECT 1;
SQL

cat > "$SCRATCH_REPO/db/tests/sample_smoke.sql" <<'SQL'
-- Minimal fixture SQL test with a "test" and "pass" column.
SELECT 'sample_smoke' AS test, true AS pass;
SQL

# --- Stubs on PATH ---------------------------------------------------------
# psql: records its invocation (useful for diagnostics) and exits 0
# without ever connecting to a real database.
cat > "$STUB_BIN_DIR/psql" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PSQL_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/psql"

# python3: stands in for `python3 scripts/ingest_test_csv.py RUN_ID ...`
# and emits the minimum normalized CSV run_tests.sh needs to proceed.
cat > "$STUB_BIN_DIR/python3" <<'STUB'
#!/usr/bin/env bash
run_id="${2:-stub_run}"
printf 'run_id,suite,test,pass,payload\n'
printf '%s,stub_suite,stub_test,true,{}\n' "$run_id"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/python3"

# --- Run run_tests.sh from the scratch repo root ---------------------------
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
  echo "----- recorded psql invocations -----" >&2
  cat "$PSQL_LOG" >&2
  echo "--------------------------------------" >&2
  exit 1
}

# --- Part 1: behavioral assertions -----------------------------------------

# The script must run to completion. With the trailing workflow YAML
# present, Bash reaches "on:" after the last psql summary call and this
# exits non-zero (127, command not found).
if [[ $RUN_STATUS -ne 0 ]]; then
  fail "scripts/run_tests.sh exited with status $RUN_STATUS (expected 0)"
fi

# Belt-and-suspenders: even if something else caused a non-127 exit code,
# explicitly check the captured output for the exact "command not found"
# signatures Bash produces when it tries to execute workflow YAML lines.
for signature in \
  "on:: command not found" \
  "schedule:: command not found" \
  "push:: command not found" \
  "pull_request:: command not found"
do
  if grep -qF -- "$signature" "$TMP_DIR/run_tests.stdout"; then
    fail "found '${signature}' in output -- embedded workflow YAML is being executed as shell"
  fi
done

echo "PASS (behavioral): scripts/run_tests.sh ran to completion with no sign of"
echo "                    embedded workflow YAML being interpreted as commands."

# --- Part 2: static guard on the real script source -------------------------
# Narrow, line-anchored checks so ordinary comments or prose mentioning
# "push" or "schedule" are never flagged -- only bare top-level YAML
# trigger-declaration lines are. This supplements (does not replace) the
# behavioral check above.
STATIC_FAIL=0
STATIC_HITS=""

check_pattern() {
  local pattern="$1" label="$2" hits
  hits="$(grep -nE -- "$pattern" "$REAL_RUN_TESTS" || true)"
  if [[ -n "$hits" ]]; then
    STATIC_FAIL=1
    STATIC_HITS+=$'\n'"[$label] $hits"
  fi
}

check_pattern '^on:[[:space:]]*$'                        "bare 'on:' line"
check_pattern '^[[:space:]]*schedule:[[:space:]]*$'       "bare 'schedule:' line"
check_pattern '^[[:space:]]*-[[:space:]]*cron:'           "'- cron:' line"
check_pattern '^[[:space:]]*push:[[:space:]]*$'           "bare 'push:' line"
check_pattern '^[[:space:]]*pull_request:[[:space:]]*$'   "bare 'pull_request:' line"

if [[ $STATIC_FAIL -ne 0 ]]; then
  echo "FAIL: scripts/run_tests.sh appears to contain embedded GitHub Actions" >&2
  echo "trigger declarations:" >&2
  echo "$STATIC_HITS" >&2
  exit 1
fi

echo "PASS (static): scripts/run_tests.sh contains no embedded GitHub Actions"
echo "               trigger declarations (on:/schedule:/- cron:/push:/pull_request:)."
