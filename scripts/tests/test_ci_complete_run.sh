#!/usr/bin/env bash
# Database-backed proof that the committed CI fixture
# (tests/fixtures/ci/complete_snapshot/) actually migrates, loads, and
# validates end to end against a real PostgreSQL schema -- the
# database-backed counterpart to the database-free
# scripts/tests/test_ci_fixture.py (which can only check the fixture's
# own internal structure, never that it actually loads or passes SQL
# validation).
#
# *** Requires a real, DISPOSABLE PostgreSQL test database via PG_DSN. ***
# PG_DSN must point to a SAFE, DISPOSABLE test database -- never
# production, staging, or any shared database. This script creates
# three uniquely-named, randomly-suffixed schemas (core/terminology/ops
# -- never "public", "tuva", "tuva_term", "tuva_ops", or any other
# reserved/production-like name) and drops only those exact schemas on
# exit, success or failure. It never touches any other schema.
#
# What this proves that scripts/tests/test_ci_fixture.py cannot:
#   1. migration 0001 (and 0002) apply cleanly to a fresh schema;
#   2. every fixture CSV's header exactly matches the migrated schema's
#      real column set, in ordinal order (via
#      `scripts/generate_ci_fixture.py check`);
#   3. the real scripts/load_to_postgres.sh loads the committed fixture
#      through its normal preflight + atomic-transaction path -- NOT the
#      zero-file no-op path;
#   4. every managed table ends up with its exact expected row count;
#   5. the encounter/practitioner/location foreign-key relationships the
#      fixture claims actually join;
#   6. the real scripts/run_tests.sh executes the full db/tests/*.sql
#      suite and writes results tied to this run's own fixed RUN_ID;
#   7. every expected suite is represented, the failure count is exactly
#      zero, and migration status is current -- via
#      scripts/verify_complete_run.py, the same verifier CI itself calls;
#   8. reloading the identical fixture a second time leaves row counts
#      unchanged (the loader's truncate-and-replace design).
#
# Usage:
#   PG_DSN=postgresql://user:pass@host:port/db \
#     bash scripts/tests/test_ci_complete_run.sh
#
# (`make test-ci-complete-run` sources .env and runs this script; it is
# intentionally NOT part of `test-shell` or `test`, so it never runs
# against an unconfigured or unintended database.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FIXTURE_DIR="$REPO_ROOT/tests/fixtures/ci/complete_snapshot"

: "${PG_DSN:?PG_DSN not set. This test requires a real, DISPOSABLE test database -- set PG_DSN and re-run.}"

cd "$REPO_ROOT"

# --- 1) Generate and validate three unique, disposable schema names -------
SUFFIX="$(date -u +%Y%m%dt%H%M%Sz)_$$_${RANDOM}"
CORE_SCHEMA="tuva_ci_run_${SUFFIX}"
TERM_SCHEMA="tuva_ci_run_term_${SUFFIX}"
OPS_SCHEMA="tuva_ci_run_ops_${SUFFIX}"

for name in "$CORE_SCHEMA" "$TERM_SCHEMA" "$OPS_SCHEMA"; do
  if ! [[ "$name" =~ ^[a-z_][a-z0-9_]{2,62}$ ]]; then
    echo "FAIL: generated schema name '$name' failed identifier validation; refusing to proceed." >&2
    exit 1
  fi
  case "$name" in
    public|tuva|tuva_term|tuva_ops|information_schema|pg_catalog)
      echo "FAIL: generated schema name '$name' collides with a reserved/production schema name." >&2
      exit 1
      ;;
  esac
done

echo "Using disposable schemas: core=${CORE_SCHEMA} terminology=${TERM_SCHEMA} ops=${OPS_SCHEMA}"

RUN_ID="ci-complete-run-test-${SUFFIX}"
TMP_DIR="$(mktemp -d)"

# --- 2) Cleanup: drop ONLY these three exact schemas, on success or failure
cleanup() {
  rm -rf "$TMP_DIR"
  echo "Cleaning up disposable schemas: ${CORE_SCHEMA}, ${TERM_SCHEMA}, ${OPS_SCHEMA}"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "
    DROP SCHEMA IF EXISTS \"${CORE_SCHEMA}\" CASCADE;
    DROP SCHEMA IF EXISTS \"${TERM_SCHEMA}\" CASCADE;
    DROP SCHEMA IF EXISTS \"${OPS_SCHEMA}\" CASCADE;
  " || echo "WARN: failed to drop one or more disposable schemas; manual cleanup may be required." >&2
}
trap cleanup EXIT

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

run_py() {
  # Every Python entry point here needs psycopg from the locked venv.
  uv run python3 "$@"
}

# --- 3) Apply the real migrations to the disposable schemas ----------------
echo "--- Applying migrations to disposable schemas ---"
if ! PG_DSN="$PG_DSN" PG_SCHEMA="$CORE_SCHEMA" TERMINOLOGY_SCHEMA="$TERM_SCHEMA" OPS_SCHEMA="$OPS_SCHEMA" \
     uv run tuva-postgres migrate; then
  fail "migrations did not apply cleanly to the disposable schemas"
fi
echo "PASS: migrations applied."

# --- 4) Compare every fixture CSV header against the migrated schema -------
echo "--- Checking fixture headers against the migrated schema (ordinal order) ---"
if ! PG_DSN="$PG_DSN" run_py "$SCRIPT_DIR/../generate_ci_fixture.py" check \
     --pg-schema "$CORE_SCHEMA" --fixture-dir "$FIXTURE_DIR"; then
  fail "the committed fixture's headers do not match the migrated schema (see diff above) -- a column was likely added, removed, renamed, or reordered since the fixture was last regenerated (see scripts/generate_ci_fixture.py)"
fi
echo "PASS: every fixture CSV header matches the migrated schema exactly, in ordinal order."

# --- 5) Load the committed fixture through the real loader ------------------
echo "--- Loading the committed fixture (first load) ---"
LOAD_LOG="$TMP_DIR/load_1.log"
if ! PG_DSN="$PG_DSN" PG_SCHEMA="$CORE_SCHEMA" DATA_DIR="$FIXTURE_DIR" \
     bash "$SCRIPT_DIR/../load_to_postgres.sh" > "$LOAD_LOG" 2>&1; then
  cat "$LOAD_LOG" >&2
  fail "the real loader failed against the committed fixture"
fi
cat "$LOAD_LOG"

if grep -q "No seed CSVs found" "$LOAD_LOG"; then
  fail "the loader took its zero-file no-op path -- the committed fixture was not actually loaded"
fi
if ! grep -q "Preflight OK: all 15 managed CSV(s) present and readable" "$LOAD_LOG"; then
  fail "the loader did not report a complete preflight (expected 'Preflight OK: all 15 managed CSV(s) present and readable')"
fi
if ! grep -q "Load complete." "$LOAD_LOG"; then
  fail "the loader did not report 'Load complete.'"
fi
echo "PASS: the loader ran its real preflight + atomic-load path, not the no-data no-op path."

# --- 6) Assert every managed table has its exact expected row count --------
echo "--- Checking managed table row counts ---"
declare -a MANAGED_TABLES=(
  "practitioner" "location" "patient" "encounter" "person_id_crosswalk"
  "medical_claim" "pharmacy_claim" "eligibility" "procedure" "observation"
  "lab_result" "condition" "medication" "immunization" "appointment"
)
for t in "${MANAGED_TABLES[@]}"; do
  expected="$(( $(wc -l < "$FIXTURE_DIR/${t}.csv") - 1 ))"
  actual="$(psql "$PG_DSN" -At -c "SELECT COUNT(*) FROM \"${CORE_SCHEMA}\".\"${t}\";")"
  if [[ "$actual" != "$expected" ]]; then
    fail "${CORE_SCHEMA}.${t} has ${actual} row(s), expected ${expected} (from the committed fixture)"
  fi
done
echo "PASS: every managed table has its exact expected fixture row count."

# --- 7) Assert key foreign-key relationships join successfully -------------
echo "--- Checking key foreign-key relationships join successfully ---"
FK_JOIN_COUNT="$(psql "$PG_DSN" -At -c "
  SELECT COUNT(*)
  FROM \"${CORE_SCHEMA}\".encounter e
  JOIN \"${CORE_SCHEMA}\".patient p ON p.person_id = e.person_id
  JOIN \"${CORE_SCHEMA}\".practitioner pr ON pr.practitioner_id = e.attending_provider_id
  JOIN \"${CORE_SCHEMA}\".location l ON l.location_id = e.facility_id;
")"
if [[ "$FK_JOIN_COUNT" -lt 1 ]]; then
  fail "encounter -> patient/practitioner/location foreign-key relationships did not join (expected >= 1 row, got ${FK_JOIN_COUNT})"
fi
echo "PASS: encounter -> patient/practitioner/location relationships join successfully (${FK_JOIN_COUNT} row(s))."

# --- 8) Run the real SQL validation suite with a fixed local RUN_ID --------
echo "--- Running the real SQL validation suite (RUN_ID=${RUN_ID}) ---"
if ! PG_DSN="$PG_DSN" PG_SCHEMA="$CORE_SCHEMA" TERMINOLOGY_SCHEMA="$TERM_SCHEMA" RUN_ID="$RUN_ID" \
     bash "$SCRIPT_DIR/../run_tests.sh" > "$TMP_DIR/run_tests.log" 2>&1; then
  cat "$TMP_DIR/run_tests.log" >&2
  fail "scripts/run_tests.sh failed"
fi
cat "$TMP_DIR/run_tests.log"
echo "PASS: the SQL validation suite executed."

# --- 9) Verify the complete run (results, suites, failures, migration status)
echo "--- Verifying the complete run ---"
if ! PG_DSN="$PG_DSN" run_py "$SCRIPT_DIR/../verify_complete_run.py" \
     --pg-schema "$CORE_SCHEMA" --ops-schema "$OPS_SCHEMA" --run-id "$RUN_ID" \
     --fixture-dir "$FIXTURE_DIR"; then
  fail "scripts/verify_complete_run.py reported the run as incomplete or failing (see output above)"
fi
echo "PASS: verify_complete_run.py confirms a complete, passing run."

# --- 10) Reload the identical fixture; row counts must not change ----------
echo "--- Reloading the identical fixture (second load) ---"
LOAD_LOG_2="$TMP_DIR/load_2.log"
if ! PG_DSN="$PG_DSN" PG_SCHEMA="$CORE_SCHEMA" DATA_DIR="$FIXTURE_DIR" \
     bash "$SCRIPT_DIR/../load_to_postgres.sh" > "$LOAD_LOG_2" 2>&1; then
  cat "$LOAD_LOG_2" >&2
  fail "the second (identical) load failed -- reloading the same snapshot should always be safely retryable"
fi
for t in "${MANAGED_TABLES[@]}"; do
  expected="$(( $(wc -l < "$FIXTURE_DIR/${t}.csv") - 1 ))"
  actual="$(psql "$PG_DSN" -At -c "SELECT COUNT(*) FROM \"${CORE_SCHEMA}\".\"${t}\";")"
  if [[ "$actual" != "$expected" ]]; then
    fail "after reloading the identical fixture, ${CORE_SCHEMA}.${t} has ${actual} row(s), expected ${expected} unchanged (rows should replace, not accumulate)"
  fi
done
echo "PASS: reloading the identical fixture left every managed table's row count unchanged."

echo ""
echo "PASS: the committed CI fixture migrates, loads through the real loader (no no-op path),"
echo "      produces the expected row counts, its key relationships join, the real SQL"
echo "      validation suite executes with zero failures across every expected suite, and"
echo "      migration status is current -- against a real, disposable PostgreSQL database."
