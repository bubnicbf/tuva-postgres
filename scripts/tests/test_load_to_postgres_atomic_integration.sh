#!/usr/bin/env bash
# PostgreSQL integration test for scripts/load_to_postgres.sh's atomic
# snapshot-replacement behavior.
#
# *** Requires a real PostgreSQL connection. ***
# PG_DSN must point to a SAFE, DISPOSABLE test database -- never a
# production database. This script only ever creates and drops one
# uniquely-named temporary schema; it never touches "public", "tuva", or
# any other schema, and never drops or truncates anything outside that
# one generated name.
#
# What this proves that the dependency-free
# scripts/tests/test_load_to_postgres_atomic.sh cannot (it only inspects
# the *generated* command stream against a stubbed psql, never a real
# server):
#   * loading a snapshot twice does not double row counts or raise
#     duplicate-key errors;
#   * a failure partway through a snapshot replacement (here, a deferred
#     foreign-key violation caught at COMMIT) leaves the *previous*,
#     already-committed snapshot fully intact -- Postgres actually rolls
#     back the truncate together with the copies, not just in theory.
#
# Usage:
#   PG_DSN=postgresql://user:pass@host:port/db \
#     bash scripts/tests/test_load_to_postgres_atomic_integration.sh
#
# (The Makefile's `test-load-integration` target sources .env and runs
# this script; it is intentionally NOT part of `test-shell` or `test`, so
# it never runs against an unconfigured or unintended database.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOADER="$REPO_ROOT/scripts/load_to_postgres.sh"

source "$REPO_ROOT/scripts/lib/postgres_identifiers.sh"

: "${PG_DSN:?PG_DSN not set. This integration test requires a real, DISPOSABLE test database -- set PG_DSN and re-run.}"

# --- 1)-4) Generate and validate a unique, disposable schema name ---------
SCHEMA_NAME="tuva_load_test_$(date -u +%Y%m%dt%H%M%Sz)_$$_${RANDOM}"

validate_postgres_identifier "$SCHEMA_NAME" "generated schema name"
case "$SCHEMA_NAME" in
  public|tuva|tuva_term|information_schema|pg_catalog)
    echo "FAIL: generated schema name '$SCHEMA_NAME' collides with a reserved/production schema name." >&2
    exit 1
    ;;
esac

echo "Using disposable temporary schema: ${SCHEMA_NAME}"

TMP_DIR="$(mktemp -d)"

# --- 5) Cleanup: drop ONLY this exact temporary schema, on success or failure
cleanup() {
  rm -rf "$TMP_DIR"
  echo "Cleaning up temporary schema: ${SCHEMA_NAME}"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS \"${SCHEMA_NAME}\" CASCADE;" \
    || echo "WARN: failed to drop temporary schema ${SCHEMA_NAME}; manual cleanup may be required." >&2
}
trap cleanup EXIT

psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA \"${SCHEMA_NAME}\";"

# --- 6)-7) Minimal fixture tables using the managed table names -------------
# Simplified on purpose: this test validates loader transaction behavior
# (truncate-together, single-session commit/rollback, retry safety), not
# the full real column/constraint set from the migration 0001 baseline DDL
# under db/migrations/sql/0001_baseline/. Every table
# the loader manages gets a fixture row; only patient/encounter/
# practitioner are referenced by foreign keys, matching the real schema's
# reference graph. All FKs are DEFERRABLE INITIALLY DEFERRED, matching the
# real DDL, so the loader's single combined TRUNCATE works without CASCADE.
psql "$PG_DSN" -v ON_ERROR_STOP=1 -v schema="$SCHEMA_NAME" -c "
  CREATE TABLE \"${SCHEMA_NAME}\".practitioner (practitioner_id varchar PRIMARY KEY, name varchar);
  CREATE TABLE \"${SCHEMA_NAME}\".location     (location_id     varchar PRIMARY KEY, name varchar);
  CREATE TABLE \"${SCHEMA_NAME}\".patient      (person_id       varchar PRIMARY KEY, name varchar);

  CREATE TABLE \"${SCHEMA_NAME}\".encounter (
    encounter_id varchar PRIMARY KEY,
    person_id    varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".person_id_crosswalk (
    crosswalk_id varchar PRIMARY KEY,
    person_id    varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".medical_claim (
    medical_claim_id varchar PRIMARY KEY,
    person_id        varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)   DEFERRABLE INITIALLY DEFERRED,
    encounter_id      varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".pharmacy_claim (
    pharmacy_claim_id varchar PRIMARY KEY,
    person_id         varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".eligibility (
    eligibility_id varchar PRIMARY KEY,
    person_id      varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".procedure (
    procedure_id    varchar PRIMARY KEY,
    person_id       varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)       DEFERRABLE INITIALLY DEFERRED,
    encounter_id    varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id)    DEFERRABLE INITIALLY DEFERRED,
    practitioner_id varchar REFERENCES \"${SCHEMA_NAME}\".practitioner(practitioner_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".observation (
    observation_id varchar PRIMARY KEY,
    person_id      varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)    DEFERRABLE INITIALLY DEFERRED,
    encounter_id   varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".lab_result (
    lab_result_id varchar PRIMARY KEY,
    person_id     varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)    DEFERRABLE INITIALLY DEFERRED,
    encounter_id  varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".condition (
    condition_id varchar PRIMARY KEY,
    person_id    varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)    DEFERRABLE INITIALLY DEFERRED,
    encounter_id varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".medication (
    medication_id   varchar PRIMARY KEY,
    person_id       varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)       DEFERRABLE INITIALLY DEFERRED,
    encounter_id    varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id)    DEFERRABLE INITIALLY DEFERRED,
    practitioner_id varchar REFERENCES \"${SCHEMA_NAME}\".practitioner(practitioner_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".immunization (
    immunization_id varchar PRIMARY KEY,
    person_id       varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)       DEFERRABLE INITIALLY DEFERRED,
    encounter_id    varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id)    DEFERRABLE INITIALLY DEFERRED,
    practitioner_id varchar REFERENCES \"${SCHEMA_NAME}\".practitioner(practitioner_id) DEFERRABLE INITIALLY DEFERRED
  );

  CREATE TABLE \"${SCHEMA_NAME}\".appointment (
    appointment_id  varchar PRIMARY KEY,
    person_id       varchar REFERENCES \"${SCHEMA_NAME}\".patient(person_id)       DEFERRABLE INITIALLY DEFERRED,
    encounter_id    varchar REFERENCES \"${SCHEMA_NAME}\".encounter(encounter_id)    DEFERRABLE INITIALLY DEFERRED,
    practitioner_id varchar REFERENCES \"${SCHEMA_NAME}\".practitioner(practitioner_id) DEFERRABLE INITIALLY DEFERRED
  );
"

declare -a MANAGED_TABLES=(
  "practitioner" "location" "patient" "encounter" "person_id_crosswalk"
  "medical_claim" "pharmacy_claim" "eligibility" "procedure" "observation"
  "lab_result" "condition" "medication" "immunization" "appointment"
)

# --- 8) A complete, valid, matching CSV set ---------------------------------
DATA_DIR="$TMP_DIR/data"
mkdir -p "$DATA_DIR"

cat > "$DATA_DIR/practitioner.csv" <<'CSV'
practitioner_id,name
prac-1,Dr. One
CSV
cat > "$DATA_DIR/location.csv" <<'CSV'
location_id,name
loc-1,Main Clinic
CSV
cat > "$DATA_DIR/patient.csv" <<'CSV'
person_id,name
p-1,Alice
p-2,Bob
CSV
cat > "$DATA_DIR/encounter.csv" <<'CSV'
encounter_id,person_id
enc-1,p-1
enc-2,p-2
CSV
cat > "$DATA_DIR/person_id_crosswalk.csv" <<'CSV'
crosswalk_id,person_id
xwalk-1,p-1
CSV
cat > "$DATA_DIR/medical_claim.csv" <<'CSV'
medical_claim_id,person_id,encounter_id
mc-1,p-1,enc-1
CSV
cat > "$DATA_DIR/pharmacy_claim.csv" <<'CSV'
pharmacy_claim_id,person_id
rx-1,p-1
CSV
cat > "$DATA_DIR/eligibility.csv" <<'CSV'
eligibility_id,person_id
elig-1,p-1
CSV
cat > "$DATA_DIR/procedure.csv" <<'CSV'
procedure_id,person_id,encounter_id,practitioner_id
proc-1,p-1,enc-1,prac-1
CSV
cat > "$DATA_DIR/observation.csv" <<'CSV'
observation_id,person_id,encounter_id
obs-1,p-1,enc-1
CSV
cat > "$DATA_DIR/lab_result.csv" <<'CSV'
lab_result_id,person_id,encounter_id
lab-1,p-1,enc-1
CSV
cat > "$DATA_DIR/condition.csv" <<'CSV'
condition_id,person_id,encounter_id
cond-1,p-1,enc-1
CSV
cat > "$DATA_DIR/medication.csv" <<'CSV'
medication_id,person_id,encounter_id,practitioner_id
med-1,p-1,enc-1,prac-1
CSV
cat > "$DATA_DIR/immunization.csv" <<'CSV'
immunization_id,person_id,encounter_id,practitioner_id
imm-1,p-1,enc-1,prac-1
CSV
cat > "$DATA_DIR/appointment.csv" <<'CSV'
appointment_id,person_id,encounter_id,practitioner_id
appt-1,p-1,enc-1,prac-1
CSV

run_loader() {
  PG_DSN="$PG_DSN" PG_SCHEMA="$SCHEMA_NAME" DATA_DIR="$DATA_DIR" bash "$LOADER"
}

row_count() {
  local table="$1"
  psql "$PG_DSN" -At -c "SELECT COUNT(*) FROM \"${SCHEMA_NAME}\".\"${table}\";"
}

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

# --- 9) Run the loader once --------------------------------------------------
echo "--- First load ---"
if ! run_loader; then
  fail "first load failed against a complete, valid dataset"
fi

# --- 10) Confirm expected rows exist ----------------------------------------
declare -A EXPECTED_FIRST=(
  [practitioner]=1 [location]=1 [patient]=2 [encounter]=2
  [person_id_crosswalk]=1 [medical_claim]=1 [pharmacy_claim]=1
  [eligibility]=1 [procedure]=1 [observation]=1 [lab_result]=1
  [condition]=1 [medication]=1 [immunization]=1 [appointment]=1
)
for t in "${MANAGED_TABLES[@]}"; do
  actual="$(row_count "$t")"
  expected="${EXPECTED_FIRST[$t]}"
  if [[ "$actual" != "$expected" ]]; then
    fail "after first load, ${t} has ${actual} row(s), expected ${expected}"
  fi
done
echo "PASS: first load produced the expected row counts in every managed table."

# --- 11)-13) Run the exact same load again; row counts must not change -----
echo "--- Second load (identical CSVs) ---"
if ! run_loader; then
  fail "second load of the identical snapshot failed (should be safely retryable, no duplicate-key errors)"
fi
for t in "${MANAGED_TABLES[@]}"; do
  actual="$(row_count "$t")"
  expected="${EXPECTED_FIRST[$t]}"
  if [[ "$actual" != "$expected" ]]; then
    fail "after retrying the identical load, ${t} has ${actual} row(s), expected ${expected} (rows should replace, not accumulate)"
  fi
done
echo "PASS: retrying the identical snapshot succeeded with no duplicate-key errors and unchanged row counts."

# --- 14)-17) Intentionally invalid CSV for a table loaded after others -----
# eligibility is loaded after practitioner/location/patient/encounter/
# person_id_crosswalk/medical_claim/pharmacy_claim -- i.e. well after "at
# least one other table". Point its person_id at a patient that does not
# exist. Because the foreign key is DEFERRABLE INITIALLY DEFERRED, the
# \copy itself succeeds; the violation is only caught when the deferred
# constraint is checked at COMMIT, which is exactly the failure mode this
# loader's single-transaction design must roll back correctly.
cat > "$DATA_DIR/eligibility.csv" <<'CSV'
eligibility_id,person_id
elig-1,p-does-not-exist
CSV

echo "--- Third load (intentionally invalid eligibility.csv -- deferred FK violation) ---"
set +e
run_loader
THIRD_STATUS=$?
set -e

if [[ "$THIRD_STATUS" -eq 0 ]]; then
  fail "loader exited 0 despite an invalid foreign key in eligibility.csv (expected a rollback failure)"
fi
echo "PASS: the loader correctly failed (exit ${THIRD_STATUS}) on an invalid foreign key reference."

# The previously committed snapshot (from the second load) must remain
# fully intact in every table -- proving the failed run's TRUNCATE was
# rolled back along with the copies, not partially applied.
for t in "${MANAGED_TABLES[@]}"; do
  actual="$(row_count "$t")"
  expected="${EXPECTED_FIRST[$t]}"
  if [[ "$actual" != "$expected" ]]; then
    fail "after the failed load, ${t} has ${actual} row(s), expected ${expected} (the prior snapshot must remain intact -- the loader must not leave truncated or partially replaced tables)"
  fi
done
echo "PASS: the previously committed snapshot remains fully intact in every table after the failed load;"
echo "      no table was left truncated or partially replaced."

# --- 18)-19) Restore/discard fixtures and drop schema -----------------------
# TMP_DIR (including the corrupted eligibility.csv) and the temporary
# schema are both removed by the cleanup trap on exit, success or failure.

echo ""
echo "PASS: scripts/load_to_postgres.sh loads and re-loads a snapshot atomically against a"
echo "      real PostgreSQL database, and a deferred constraint failure rolls back the"
echo "      entire replacement, leaving the previous snapshot intact."
