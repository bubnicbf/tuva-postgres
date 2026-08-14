#!/usr/bin/env bash
# Regression test for scripts/load_to_postgres.sh
#
# Guards against the obsolete post-load "db/seed.sql" step being
# reintroduced into the loader. That file belonged to a legacy schema
# model (tuva.claim, tuva.claim_line, payer_id, charge_amt, paid_amt,
# tuva.v_claim_summary) that no longer matches the current per-table
# definitions under db/tables/ (medical_claim, pharmacy_claim,
# eligibility, etc). Running it after loading current CSVs could fail
# against nonexistent legacy tables/columns.
#
# No real PostgreSQL server, credentials, network, or production CSV data
# are required: an executable `psql` stub is put first on PATH. It just
# records each invocation and exits 0, never connecting to a database.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOADER="$REPO_ROOT/scripts/load_to_postgres.sh"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

STUB_BIN_DIR="$TMP_DIR/bin"
DATA_DIR="$TMP_DIR/data"
PSQL_LOG="$TMP_DIR/psql_invocations.log"
mkdir -p "$STUB_BIN_DIR" "$DATA_DIR"
: > "$PSQL_LOG"

# --- Minimal fixture -------------------------------------------------------
# psql is stubbed, so the CSV content itself is never parsed by a real
# database -- it just needs a header + one row so the loader takes its
# "file present" branch for medical_claim.
cat > "$DATA_DIR/medical_claim.csv" <<'CSV'
claim_id,patient_id,claim_line_number
claim-1,patient-1,1
CSV

# --- Build the psql stub ----------------------------------------------------
# Records every invocation (as a single space-joined line) to PSQL_LOG and
# exits 0 without ever touching a real database.
cat > "$STUB_BIN_DIR/psql" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PSQL_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/psql"

# --- Run load_to_postgres.sh with the stub on PATH --------------------------
EXPECTED_SCHEMA="regression_test_schema"

set +e
PATH="$STUB_BIN_DIR:$PATH" \
  PG_DSN="postgresql://test:test@localhost:5432/testdb" \
  PG_SCHEMA="$EXPECTED_SCHEMA" \
  DATA_DIR="$DATA_DIR" \
  PSQL_LOG="$PSQL_LOG" \
  bash "$LOADER" > "$TMP_DIR/loader.stdout" 2>&1
RUN_STATUS=$?
set -e

fail() {
  echo "FAIL: $1" >&2
  echo "----- loader output -----" >&2
  cat "$TMP_DIR/loader.stdout" >&2
  echo "----- recorded psql invocations -----" >&2
  cat "$PSQL_LOG" >&2
  echo "--------------------------------------" >&2
  exit 1
}

# --- Assertions --------------------------------------------------------------

# 1) The loader must exit successfully.
if [[ $RUN_STATUS -ne 0 ]]; then
  fail "scripts/load_to_postgres.sh exited with status $RUN_STATUS (expected 0)"
fi

# 2) The loader must create the configured schema.
if ! grep -qF -- "CREATE SCHEMA IF NOT EXISTS ${EXPECTED_SCHEMA};" "$PSQL_LOG"; then
  fail "no psql invocation created schema ${EXPECTED_SCHEMA}"
fi

# 3) & 4) The loader must \copy into the current medical_claim table, in the
#         configured schema, since the fixture CSV is present.
if ! grep -qF -- "\\copy ${EXPECTED_SCHEMA}.medical_claim FROM" "$PSQL_LOG"; then
  fail "no psql invocation copied into ${EXPECTED_SCHEMA}.medical_claim"
fi

# 5) No invocation may reference the obsolete seed file.
if grep -qF -- "-f db/seed.sql" "$PSQL_LOG"; then
  fail "found a psql invocation referencing the obsolete -f db/seed.sql"
fi

# 6) No invocation may reference legacy schema objects that only ever lived
#    in db/seed.sql.
for legacy in "tuva.claim" "tuva.claim_line" "v_claim_summary"; do
  if grep -qF -- "$legacy" "$PSQL_LOG"; then
    fail "found a psql invocation referencing legacy object '${legacy}'"
  fi
done

# 7) The loader must no longer print the obsolete seed status message.
if grep -qF -- "Running post-load seed.sql" "$TMP_DIR/loader.stdout"; then
  fail "loader output still contains 'Running post-load seed.sql'"
fi

# 8) The loader must still report successful completion.
if ! grep -qF -- "Load complete." "$TMP_DIR/loader.stdout"; then
  fail "loader output does not contain 'Load complete.'"
fi

echo "PASS: scripts/load_to_postgres.sh creates schema ${EXPECTED_SCHEMA}, copies"
echo "      medical_claim.csv into ${EXPECTED_SCHEMA}.medical_claim, never invokes"
echo "      db/seed.sql or any legacy schema object, and reports 'Load complete.'."
