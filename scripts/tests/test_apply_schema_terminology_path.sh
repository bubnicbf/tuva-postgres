#!/usr/bin/env bash
# Regression test for scripts/apply_schema.sh
#
# Verifies (by behavior, not by grepping the script source) that
# apply_schema.sh loads terminology SQL files from db/tables/terminology.
# This test would FAIL against the old, buggy "db/terminology" path,
# because apply_folder() silently skips folders with no matching *.sql
# files -- so no psql invocation would ever reference a terminology file.
#
# No real PostgreSQL server is required: a stub `psql` executable is put
# first on PATH, and it just records the arguments it was called with.
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
mkdir -p "$STUB_BIN_DIR"
: > "$PSQL_LOG"

# --- Build the psql stub -----------------------------------------------
# Records every invocation (as a single space-joined line) to PSQL_LOG and
# exits 0 without touching a real database.
cat > "$STUB_BIN_DIR/psql" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PSQL_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/psql"

# --- Run apply_schema.sh with the stub psql on PATH ---------------------
EXPECTED_TERM_SCHEMA="regression_test_term_schema"

set +e
PATH="$STUB_BIN_DIR:$PATH" \
  PG_DSN="postgresql://test:test@localhost:5432/testdb" \
  PG_SCHEMA="regression_test_schema" \
  TERMINOLOGY_SCHEMA="$EXPECTED_TERM_SCHEMA" \
  PSQL_LOG="$PSQL_LOG" \
  bash "$REPO_ROOT/scripts/apply_schema.sh" > "$TMP_DIR/apply_schema.stdout" 2>&1
RUN_STATUS=$?
set -e

if [[ $RUN_STATUS -ne 0 ]]; then
  echo "FAIL: scripts/apply_schema.sh exited with status $RUN_STATUS" >&2
  echo "----- apply_schema.sh output -----" >&2
  cat "$TMP_DIR/apply_schema.stdout" >&2
  exit 1
fi

# --- Assertions -----------------------------------------------------------
# 1) At least one psql invocation must apply a SQL file from
#    db/tables/terminology.
TERMINOLOGY_INVOCATIONS="$(grep -F "db/tables/terminology/" "$PSQL_LOG" || true)"

if [[ -z "$TERMINOLOGY_INVOCATIONS" ]]; then
  echo "FAIL: no psql invocation referenced a file under db/tables/terminology." >&2
  echo "This means apply_schema.sh is not loading terminology SQL files from" >&2
  echo "the correct directory (regression of the db/terminology path bug)." >&2
  echo "----- recorded psql invocations -----" >&2
  cat "$PSQL_LOG" >&2
  echo "--------------------------------------" >&2
  exit 1
fi

# 2) Those terminology invocations must carry the terminology_schema psql
#    variable with the expected value.
if ! grep -qF -- "-v terminology_schema=${EXPECTED_TERM_SCHEMA}" <<<"$TERMINOLOGY_INVOCATIONS"; then
  echo "FAIL: psql invocations for db/tables/terminology did not include" >&2
  echo "'-v terminology_schema=${EXPECTED_TERM_SCHEMA}'." >&2
  echo "----- terminology psql invocations -----" >&2
  echo "$TERMINOLOGY_INVOCATIONS" >&2
  echo "-----------------------------------------" >&2
  exit 1
fi

echo "PASS: apply_schema.sh applies terminology SQL files from db/tables/terminology"
echo "      with -v terminology_schema=${EXPECTED_TERM_SCHEMA}."
