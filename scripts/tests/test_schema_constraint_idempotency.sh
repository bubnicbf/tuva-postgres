#!/usr/bin/env bash
# PostgreSQL integration test: proves the core-table DDL under db/tables/
# can be applied twice in a row to the same schema without error, and
# that every expected foreign key ends up present exactly once, on the
# right table, in the right schema, still deferrable and initially
# deferred.
#
# *** Requires a real PostgreSQL connection. ***
# PG_DSN must point to a SAFE, DISPOSABLE test database -- never a
# production database. This script only ever creates and drops one
# uniquely-named temporary schema; it never touches "public", "tuva", or
# any other schema, and never drops or truncates anything outside that
# one generated name.
#
# Usage:
#   PG_DSN=postgresql://user:pass@host:port/db \
#     bash scripts/tests/test_schema_constraint_idempotency.sh
#
# (The Makefile's `test-schema-idempotency` target sources .env and runs
# this script; it is intentionally NOT part of `test-shell` or `test`, so
# it never runs against an unconfigured or unintended database.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${PG_DSN:?PG_DSN not set. This integration test requires a real, DISPOSABLE test database -- set PG_DSN and re-run.}"

# --- 1)-3) Generate and validate a unique, disposable schema name ---------
SCHEMA_NAME="tuva_idem_test_$(date -u +%Y%m%dt%H%M%Sz)_$$_${RANDOM}"

if ! [[ "$SCHEMA_NAME" =~ ^[a-z_][a-z0-9_]{2,62}$ ]]; then
  echo "FAIL: generated schema name '$SCHEMA_NAME' failed identifier validation; refusing to proceed." >&2
  exit 1
fi
# Extra belt-and-suspenders: never allow this to collide with a real,
# meaningful schema name.
case "$SCHEMA_NAME" in
  public|tuva|tuva_term|information_schema|pg_catalog)
    echo "FAIL: generated schema name '$SCHEMA_NAME' collides with a reserved/production schema name." >&2
    exit 1
    ;;
esac

echo "Using disposable temporary schema: ${SCHEMA_NAME}"

# --- 4) Create the temporary schema ----------------------------------------
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA \"${SCHEMA_NAME}\";"

# --- 5)-6) Cleanup trap: drop ONLY this exact temporary schema -------------
CATALOG_TMP=""
cleanup() {
  if [[ -n "$CATALOG_TMP" && -f "$CATALOG_TMP" ]]; then
    rm -f "$CATALOG_TMP"
  fi
  echo "Cleaning up temporary schema: ${SCHEMA_NAME}"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS \"${SCHEMA_NAME}\" CASCADE;" \
    || echo "WARN: failed to drop temporary schema ${SCHEMA_NAME}; manual cleanup may be required." >&2
}
trap cleanup EXIT

# --- 7) Dependency order: independent tables first, then dependents -------
# patient and practitioner have no FKs of their own. encounter references
# both, so it must exist before anything that references *it*. All
# remaining files reference only patient/practitioner/encounter, not each
# other, so their relative order doesn't matter.
declare -a TABLE_FILES=(
  "patient"
  "practitioner"
  "encounter"
  "appointment"
  "condition"
  "eligibility"
  "immunization"
  "lab_result"
  "medical_claim"
  "medication"
  "observation"
  "person_id_crosswalk"
  "pharmacy_claim"
  "procedure"
)

apply_all() {
  local label="$1"
  echo "--- Applying core table DDL (${label}) to ${SCHEMA_NAME} ---"
  for t in "${TABLE_FILES[@]}"; do
    local f="${REPO_ROOT}/db/tables/${t}.sql"
    if [[ ! -f "$f" ]]; then
      echo "FAIL: expected table file not found: $f" >&2
      exit 1
    fi
    echo "Applying: db/tables/${t}.sql"
    psql "$PG_DSN" -v ON_ERROR_STOP=1 -v schema="$SCHEMA_NAME" -f "$f"
  done
}

# --- 8) First application ---------------------------------------------------
apply_all "first application"

# --- 9) Second application to the SAME schema -- the idempotency proof ----
if ! apply_all "second application"; then
  echo "FAIL: re-applying the same DDL to ${SCHEMA_NAME} a second time failed." >&2
  exit 1
fi
echo "Second application succeeded (constraints were not recreated)."

# --- 10) Catalog verification -----------------------------------------------
# table:constraint pairs, matching scripts/tests/test_constraint_idempotency_guards.py
declare -a EXPECTED=(
  "appointment:appt_person_fk"
  "appointment:appt_encounter_fk"
  "appointment:appt_practitioner_fk"
  "condition:condition_person_fk"
  "condition:condition_encounter_fk"
  "eligibility:elig_person_fk"
  "encounter:encounter_person_fk"
  "encounter:encounter_attending_pr_fk"
  "immunization:imm_person_fk"
  "immunization:imm_encounter_fk"
  "immunization:imm_practitioner_fk"
  "lab_result:lab_result_person_fk"
  "lab_result:lab_result_encounter_fk"
  "medical_claim:mc_person_fk"
  "medical_claim:mc_encounter_fk"
  "medication:med_person_fk"
  "medication:med_encounter_fk"
  "medication:med_practitioner_fk"
  "observation:observation_person_fk"
  "observation:observation_encounter_fk"
  "person_id_crosswalk:pxw_person_fk"
  "pharmacy_claim:rx_person_fk"
  "procedure:procedure_person_fk"
  "procedure:procedure_encounter_fk"
  "procedure:procedure_practitioner_fk"
)

CATALOG_TMP="$(mktemp)"

psql "$PG_DSN" -v ON_ERROR_STOP=1 -At -F'|' -c "
  SELECT r.relname, c.conname, c.contype, c.condeferrable, c.condeferred, COUNT(*) OVER (PARTITION BY r.relname, c.conname)
  FROM pg_constraint c
  JOIN pg_class r ON r.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = r.relnamespace
  WHERE n.nspname = '${SCHEMA_NAME}'
    AND c.contype = 'f';
" > "$CATALOG_TMP"

fail_catalog() {
  echo "FAIL: $1" >&2
  echo "----- catalog rows (table|constraint|type|deferrable|deferred|count) -----" >&2
  cat "$CATALOG_TMP" >&2
  echo "----------------------------------------------------------------------------" >&2
  exit 1
}

FAILURES=0
for pair in "${EXPECTED[@]}"; do
  table="${pair%%:*}"
  conname="${pair##*:}"

  row="$(awk -F'|' -v t="$table" -v c="$conname" '$1==t && $2==c' "$CATALOG_TMP")"

  if [[ -z "$row" ]]; then
    echo "MISSING: expected foreign key ${conname} on ${table} not found in ${SCHEMA_NAME}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  cnt="$(echo "$row" | awk -F'|' '{print $6}')"
  contype="$(echo "$row" | awk -F'|' '{print $3}')"
  deferrable="$(echo "$row" | awk -F'|' '{print $4}')"
  deferred="$(echo "$row" | awk -F'|' '{print $5}')"

  if [[ "$cnt" != "1" ]]; then
    echo "DUPLICATE: ${conname} on ${table} appears ${cnt} time(s) in ${SCHEMA_NAME} (expected exactly 1)" >&2
    FAILURES=$((FAILURES + 1))
  fi
  if [[ "$contype" != "f" ]]; then
    echo "WRONG TYPE: ${conname} on ${table} has contype='${contype}' (expected 'f' for foreign key)" >&2
    FAILURES=$((FAILURES + 1))
  fi
  if [[ "$deferrable" != "t" ]]; then
    echo "NOT DEFERRABLE: ${conname} on ${table} has condeferrable='${deferrable}' (expected 't')" >&2
    FAILURES=$((FAILURES + 1))
  fi
  if [[ "$deferred" != "t" ]]; then
    echo "NOT INITIALLY DEFERRED: ${conname} on ${table} has condeferred='${deferred}' (expected 't')" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

if [[ $FAILURES -gt 0 ]]; then
  fail_catalog "${FAILURES} catalog assertion(s) failed for schema ${SCHEMA_NAME}"
fi

echo "PASS: all ${#EXPECTED[@]} expected foreign keys exist exactly once in ${SCHEMA_NAME},"
echo "      each on the correct table, still deferrable and initially deferred,"
echo "      after applying the core table DDL twice."
