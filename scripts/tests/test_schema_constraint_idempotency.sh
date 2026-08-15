#!/usr/bin/env bash
# PostgreSQL integration test: proves the real migration runner
# (scripts/apply_schema.sh -> src/tuva_postgres/migrations.py) can be
# invoked twice in a row against the same, freshly created schemas
# without error -- the first run applies migration 0001 (baseline) and
# 0002 (operational schema) from their version-owned directories under
# db/migrations/sql/, and the second run must apply exactly zero
# migrations (checksum-matched, already-recorded) and leave the schema
# byte-for-byte unchanged. This also confirms every expected foreign key
# ends up present exactly once, on the right table, in the right schema,
# still deferrable and initially deferred.
#
# *** Requires a real PostgreSQL connection. ***
# PG_DSN must point to a SAFE, DISPOSABLE test database -- never a
# production database. This script only ever creates and drops three
# uniquely-named temporary schemas (core, terminology, ops); it never
# touches "public", "tuva", "tuva_term", "tuva_ops", or any other schema.
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

# --- 1)-3) Generate and validate unique, disposable schema names ----------
SUFFIX="$(date -u +%Y%m%dt%H%M%Sz)_$$_${RANDOM}"
SCHEMA_NAME="tuva_idem_test_${SUFFIX}"
TERM_SCHEMA_NAME="tuva_idem_test_${SUFFIX}_term"
OPS_SCHEMA_NAME="tuva_idem_test_${SUFFIX}_ops"

for name in "$SCHEMA_NAME" "$TERM_SCHEMA_NAME" "$OPS_SCHEMA_NAME"; do
  if ! [[ "$name" =~ ^[a-z_][a-z0-9_]{2,62}$ ]]; then
    echo "FAIL: generated schema name '$name' failed identifier validation; refusing to proceed." >&2
    exit 1
  fi
  # Extra belt-and-suspenders: never allow this to collide with a real,
  # meaningful schema name.
  case "$name" in
    public|tuva|tuva_term|tuva_ops|information_schema|pg_catalog)
      echo "FAIL: generated schema name '$name' collides with a reserved/production schema name." >&2
      exit 1
      ;;
  esac
done

echo "Using disposable temporary schemas: ${SCHEMA_NAME}, ${TERM_SCHEMA_NAME}, ${OPS_SCHEMA_NAME}"

# --- 4) Cleanup trap: drop ONLY these exact temporary schemas ---------------
CATALOG_TMP_1=""
CATALOG_TMP_2=""
cleanup() {
  rm -f "$CATALOG_TMP_1" "$CATALOG_TMP_2" 2>/dev/null || true
  echo "Cleaning up temporary schemas: ${SCHEMA_NAME}, ${TERM_SCHEMA_NAME}, ${OPS_SCHEMA_NAME}"
  for name in "$SCHEMA_NAME" "$TERM_SCHEMA_NAME" "$OPS_SCHEMA_NAME"; do
    psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS \"${name}\" CASCADE;" \
      || echo "WARN: failed to drop temporary schema ${name}; manual cleanup may be required." >&2
  done
}
trap cleanup EXIT

run_migrations() {
  # Delegates to the single authoritative schema-deployment path -- the
  # same script `make create-db` uses -- rather than hand-applying
  # individual DDL files. See scripts/apply_schema.sh.
  PG_DSN="$PG_DSN" \
    PG_SCHEMA="$SCHEMA_NAME" \
    TERMINOLOGY_SCHEMA="$TERM_SCHEMA_NAME" \
    OPS_SCHEMA="$OPS_SCHEMA_NAME" \
    bash "$REPO_ROOT/scripts/apply_schema.sh"
}

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

# --- 5) First application: applies migrations 0001 and 0002 ---------------
echo "--- Applying migrations (first run) via scripts/apply_schema.sh ---"
FIRST_OUT="$(run_migrations)" || fail "first migration run exited nonzero"
echo "$FIRST_OUT"
if ! grep -qE "Applied 0001|Applied [0-9]+ migration\(s\)\." <<<"$FIRST_OUT"; then
  fail "first migration run did not report applying any migrations"
fi

# --- 6) Catalog verification (before second run) ---------------------------
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

dump_catalog() {
  local out_file="$1"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -At -F'|' -c "
    SELECT r.relname, c.conname, c.contype, c.condeferrable, c.condeferred, COUNT(*) OVER (PARTITION BY r.relname, c.conname)
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE n.nspname = '${SCHEMA_NAME}'
      AND c.contype = 'f';
  " > "$out_file"
}

check_catalog() {
  local catalog_file="$1"
  local failures=0
  for pair in "${EXPECTED[@]}"; do
    table="${pair%%:*}"
    conname="${pair##*:}"

    row="$(awk -F'|' -v t="$table" -v c="$conname" '$1==t && $2==c' "$catalog_file")"

    if [[ -z "$row" ]]; then
      echo "MISSING: expected foreign key ${conname} on ${table} not found in ${SCHEMA_NAME}" >&2
      failures=$((failures + 1))
      continue
    fi

    cnt="$(echo "$row" | awk -F'|' '{print $6}')"
    contype="$(echo "$row" | awk -F'|' '{print $3}')"
    deferrable="$(echo "$row" | awk -F'|' '{print $4}')"
    deferred="$(echo "$row" | awk -F'|' '{print $5}')"

    if [[ "$cnt" != "1" ]]; then
      echo "DUPLICATE: ${conname} on ${table} appears ${cnt} time(s) in ${SCHEMA_NAME} (expected exactly 1)" >&2
      failures=$((failures + 1))
    fi
    if [[ "$contype" != "f" ]]; then
      echo "WRONG TYPE: ${conname} on ${table} has contype='${contype}' (expected 'f' for foreign key)" >&2
      failures=$((failures + 1))
    fi
    if [[ "$deferrable" != "t" ]]; then
      echo "NOT DEFERRABLE: ${conname} on ${table} has condeferrable='${deferrable}' (expected 't')" >&2
      failures=$((failures + 1))
    fi
    if [[ "$deferred" != "t" ]]; then
      echo "NOT INITIALLY DEFERRED: ${conname} on ${table} has condeferred='${deferred}' (expected 't')" >&2
      failures=$((failures + 1))
    fi
  done
  echo "$failures"
}

CATALOG_TMP_1="$(mktemp)"
dump_catalog "$CATALOG_TMP_1"
FAILURES_1="$(check_catalog "$CATALOG_TMP_1")"
if [[ "$FAILURES_1" -gt 0 ]]; then
  echo "----- catalog rows after first run (table|constraint|type|deferrable|deferred|count) -----" >&2
  cat "$CATALOG_TMP_1" >&2
  fail "${FAILURES_1} catalog assertion(s) failed for schema ${SCHEMA_NAME} after the first migration run"
fi
echo "First run: all ${#EXPECTED[@]} expected foreign keys present exactly once, deferrable and initially deferred."

# --- 7) Second application: must be a true no-op ----------------------------
echo "--- Applying migrations (second run) via scripts/apply_schema.sh ---"
SECOND_OUT="$(run_migrations)" || fail "second migration run exited nonzero (re-running an already-applied migration set must succeed as a no-op)"
echo "$SECOND_OUT"
if ! grep -qF "No pending migrations. Database is up to date." <<<"$SECOND_OUT"; then
  fail "second migration run did not report zero pending migrations -- expected a true no-op re-run"
fi
echo "Second run applied zero migrations (checksum-matched no-op)."

# --- 8) Catalog verification (after second run) -- must be unchanged -------
CATALOG_TMP_2="$(mktemp)"
dump_catalog "$CATALOG_TMP_2"
FAILURES_2="$(check_catalog "$CATALOG_TMP_2")"
if [[ "$FAILURES_2" -gt 0 ]]; then
  echo "----- catalog rows after second run (table|constraint|type|deferrable|deferred|count) -----" >&2
  cat "$CATALOG_TMP_2" >&2
  fail "${FAILURES_2} catalog assertion(s) failed for schema ${SCHEMA_NAME} after the second (no-op) migration run"
fi

if ! diff -q <(sort "$CATALOG_TMP_1") <(sort "$CATALOG_TMP_2") > /dev/null; then
  echo "----- catalog rows after first run -----" >&2
  cat "$CATALOG_TMP_1" >&2
  echo "----- catalog rows after second run -----" >&2
  cat "$CATALOG_TMP_2" >&2
  fail "foreign-key catalog changed between the first and second (no-op) migration run"
fi

echo "PASS: scripts/apply_schema.sh applied migrations 0001 and 0002 cleanly on the"
echo "      first run (all ${#EXPECTED[@]} expected foreign keys present exactly once,"
echo "      each deferrable and initially deferred), and the second run applied zero"
echo "      migrations, leaving the schema's foreign-key catalog byte-for-byte unchanged."
