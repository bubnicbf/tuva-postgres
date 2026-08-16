#!/usr/bin/env bash
# PostgreSQL integration test: proves the real, authoritative migration
# entry point (scripts/apply_schema.sh -> `python -m tuva_postgres.
# migrations` -> src/tuva_postgres/migrations.py) is truly idempotent --
# invoking it a second time against the exact same disposable schemas,
# with no history bypassed and no SQL hand-rerun, must apply ZERO
# migrations and leave everything byte-for-byte unchanged:
#
#   * the complete migration-history table (schema_migrations) -- every
#     column, not just row count: version, description, checksum,
#     applied_at, duration_ms, app_version, execution, execution_count;
#   * a comprehensive, deterministic catalog fingerprint covering every
#     schema/table/view/materialized view/column/primary key/foreign
#     key/unique constraint/check constraint/index/function/trigger the
#     migrations create, plus the migration-history table's own
#     structure;
#   * the focused foreign-key catalog (kept from the original version of
#     this test) -- every expected FK present exactly once, on the right
#     table/schema, still deferrable and initially deferred.
#
# This is the CI-blocking gate for accidental migration
# non-idempotency: failure to recognize already-applied migrations,
# duplicate history writes, unexpected execution-count changes,
# metadata-bootstrap mutations on every invocation, changed checksums or
# execution modes, repeatable migrations reapplied despite unchanged
# content, duplicate schema objects, or catalog drift caused by the
# second runner invocation.
#
# Deliberately NOT tested here: SQL-level re-execution safety for
# CHANGED repeatable migrations (that belongs to
# scripts/tests/test_migration_execution_modes.sh and tests/integration/
# test_pipeline_integration.py's execution-mode integration coverage,
# using fixture migrations -- never the real, committed ones). This test
# never bypasses schema_migrations to manually rerun one_time SQL; it
# only ever calls the real migration runner, twice, and inspects what it
# actually did.
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
# it never runs against an unconfigured or unintended database. CI runs
# it as a dedicated step -- see .github/workflows/ci.yml.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Shared ASCII identifier policy (same regex src/tuva_postgres/
# identifiers.py enforces on the Python side) -- used below to sanity-
# check this script's own generated schema names.
source "$REPO_ROOT/scripts/lib/postgres_identifiers.sh"

: "${PG_DSN:?PG_DSN not set. This integration test requires a real, DISPOSABLE test database -- set PG_DSN and re-run.}"

# --- 1)-3) Generate and validate unique, disposable schema names ----------
SUFFIX="$(date -u +%Y%m%dt%H%M%Sz)_$$_${RANDOM}"
SCHEMA_NAME="tuva_idem_test_${SUFFIX}"
TERM_SCHEMA_NAME="tuva_idem_test_${SUFFIX}_term"
OPS_SCHEMA_NAME="tuva_idem_test_${SUFFIX}_ops"

for name in "$SCHEMA_NAME" "$TERM_SCHEMA_NAME" "$OPS_SCHEMA_NAME"; do
  validate_postgres_identifier "$name" "generated schema name"
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

TMP_DIR="$(mktemp -d)"

# --- 4) Cleanup trap: drop ONLY these exact temporary schemas + tmp files --
cleanup() {
  rm -rf "$TMP_DIR" 2>/dev/null || true
  echo "Cleaning up temporary schemas: ${SCHEMA_NAME}, ${TERM_SCHEMA_NAME}, ${OPS_SCHEMA_NAME}"
  for name in "$SCHEMA_NAME" "$TERM_SCHEMA_NAME" "$OPS_SCHEMA_NAME"; do
    psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS \"${name}\" CASCADE;" \
      || echo "WARN: failed to drop temporary schema ${name}; manual cleanup may be required." >&2
  done
}
trap cleanup EXIT

fail() {
  echo "FAIL: $1" >&2
  echo "" >&2
  echo "--- diagnostics ---" >&2
  echo "First-run output:" >&2
  [[ -f "$TMP_DIR/first_run.out" ]] && cat "$TMP_DIR/first_run.out" >&2
  echo "Second-run output:" >&2
  [[ -f "$TMP_DIR/second_run.out" ]] && cat "$TMP_DIR/second_run.out" >&2
  echo "Final status output:" >&2
  [[ -f "$TMP_DIR/final_status.out" ]] && cat "$TMP_DIR/final_status.out" >&2
  if [[ -f "$TMP_DIR/history_1.txt" && -f "$TMP_DIR/history_2.txt" ]]; then
    echo "History diff (before second run vs. after second run):" >&2
    diff -u "$TMP_DIR/history_1.txt" "$TMP_DIR/history_2.txt" >&2 || true
  fi
  if [[ -f "$TMP_DIR/fingerprint_1.txt" && -f "$TMP_DIR/fingerprint_2.txt" ]]; then
    echo "Catalog fingerprint diff (before second run vs. after second run):" >&2
    diff -u "$TMP_DIR/fingerprint_1.txt" "$TMP_DIR/fingerprint_2.txt" >&2 || true
  fi
  echo "Current schema_migrations rows:" >&2
  psql "$PG_DSN" -c "SELECT * FROM \"${OPS_SCHEMA_NAME}\".schema_migrations ORDER BY version;" >&2 2>/dev/null || true
  echo "Current migration status:" >&2
  run_migrations --status >&2 2>/dev/null || true
  exit 1
}

# --- Resolve the same uv-vs-PYTHONPATH-fallback path apply_schema.sh uses,
#     so discover()/compute_checksum() are called against the exact same
#     interpreter/environment that will run the actual migrations -----------
run_python() {
  if command -v uv >/dev/null 2>&1 && [[ -f "$REPO_ROOT/uv.lock" ]]; then
    (cd "$REPO_ROOT" && uv run python3 "$@")
  else
    PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}" python3 "$@"
  fi
}

run_migrations() {
  # Delegates to the single authoritative schema-deployment path -- the
  # same script `make create-db` uses -- rather than hand-applying
  # individual DDL files or bypassing migration history. See
  # scripts/apply_schema.sh. Both the first and second pass call this
  # exact same function with the exact same arguments (never "$@" varied
  # between passes, never a different code path).
  PG_DSN="$PG_DSN" \
    PG_SCHEMA="$SCHEMA_NAME" \
    TERMINOLOGY_SCHEMA="$TERM_SCHEMA_NAME" \
    OPS_SCHEMA="$OPS_SCHEMA_NAME" \
    bash "$REPO_ROOT/scripts/apply_schema.sh" "$@"
}

psql1() {
  # -At -F'|': unaligned, tuples-only, pipe-delimited -- stable, diffable
  # text with no box-drawing/row-count footer noise.
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -At -F'|' -c "$1"
}

# --- Discover the expected migration set from the real, committed
#     manifests (never hardcoded to "two") -----------------------------------
echo "--- Discovering expected migrations from db/migrations/ ---"
# Absolute paths only -- deliberately independent of the caller's current
# working directory (unlike a bare 'src'/'db/migrations'/'.', which would
# silently resolve against whatever directory happened to invoke this
# script rather than always meaning "this repo").
run_python -c "
import sys
sys.path.insert(0, '${REPO_ROOT}/src')
from tuva_postgres.migrations import discover, compute_checksum
from pathlib import Path
for m in discover(Path('${REPO_ROOT}/db/migrations'), Path('${REPO_ROOT}')):
    print(f'{m.version}|{m.execution.value}|{compute_checksum(m)}')
" > "$TMP_DIR/expected_migrations.txt"

EXPECTED_COUNT="$(wc -l < "$TMP_DIR/expected_migrations.txt" | tr -d ' ')"
if [[ "$EXPECTED_COUNT" -lt 1 ]]; then
  fail "no migrations discovered under db/migrations/ -- expected at least one"
fi
echo "Discovered ${EXPECTED_COUNT} migration(s):"
cat "$TMP_DIR/expected_migrations.txt"

if ! grep -q "^0001|" "$TMP_DIR/expected_migrations.txt" || ! grep -q "^0002|" "$TMP_DIR/expected_migrations.txt"; then
  fail "expected migrations 0001 and 0002 to be among the discovered set -- discovery may be broken"
fi

# --- Before the first run: the disposable schemas must not already exist --
echo "--- Confirming disposable schemas are absent before the first run ---"
PRE_EXISTING="$(psql1 "
  SELECT nspname FROM pg_namespace
  WHERE nspname IN ('${SCHEMA_NAME}', '${TERM_SCHEMA_NAME}', '${OPS_SCHEMA_NAME}');
")"
if [[ -n "$PRE_EXISTING" ]]; then
  fail "disposable schema(s) already exist before the first migration run: ${PRE_EXISTING} -- refusing to silently adopt an existing schema (this test never uses --baseline-existing)"
fi
echo "Confirmed: none of the three disposable schemas exist yet."

# --- 5) First application: applies every pending migration -----------------
echo "--- Applying migrations (first run) via scripts/apply_schema.sh ---"
set +e
run_migrations > "$TMP_DIR/first_run.out" 2>&1
FIRST_STATUS=$?
set -e
cat "$TMP_DIR/first_run.out"
if [[ "$FIRST_STATUS" -ne 0 ]]; then
  fail "first migration run exited nonzero (status ${FIRST_STATUS})"
fi
if ! grep -qE "Applied [0-9]+ migration\(s\)\." "$TMP_DIR/first_run.out"; then
  fail "first migration run did not report applying any migrations"
fi
FIRST_APPLIED_COUNT="$(grep -oE "Applied [0-9]+ migration\(s\)\." "$TMP_DIR/first_run.out" | grep -oE '[0-9]+')"
if [[ "$FIRST_APPLIED_COUNT" -lt 1 ]]; then
  fail "first migration run reported applying 0 migrations -- expected all ${EXPECTED_COUNT} discovered migration(s)"
fi
echo "First run applied ${FIRST_APPLIED_COUNT} migration(s)."

# --- Assert the expected schemas now exist ----------------------------------
for name in "$SCHEMA_NAME" "$TERM_SCHEMA_NAME" "$OPS_SCHEMA_NAME"; do
  EXISTS="$(psql1 "SELECT 1 FROM pg_namespace WHERE nspname = '${name}';")"
  [[ "$EXISTS" == "1" ]] || fail "expected schema '${name}' does not exist after the first migration run"
done
echo "Confirmed: core, terminology, and ops schemas all exist after the first run."

# --- History snapshot function: full 8-column row set, deterministic order -
dump_history() {
  local out_file="$1"
  psql1 "
    SELECT version, description, checksum, applied_at, duration_ms, app_version, execution, execution_count
    FROM \"${OPS_SCHEMA_NAME}\".schema_migrations
    ORDER BY version;
  " > "$out_file"
}

# --- Comprehensive, deterministic catalog fingerprint -----------------------
# Every section is independently ORDER BY'd on stable, human-meaningful
# columns (never OIDs, physical relfilenodes, planner statistics,
# transaction IDs, or unsorted creation order). Definitions come from
# PostgreSQL's own stable rendering functions (pg_get_constraintdef,
# pg_get_indexdef, pg_views/pg_matviews' `definition` -- itself
# pg_get_viewdef under the hood, pg_get_functiondef, pg_get_triggerdef),
# so two structurally identical schemas always fingerprint identically
# regardless of when/how they were created. Internal, FK-enforcement
# triggers (tgisinternal) are explicitly excluded -- Postgres names them
# using their own OID (e.g. "RI_ConstraintTrigger_c_16400"), which is
# never stable across two independently created schemas even when the
# schemas are otherwise byte-for-byte identical.
dump_full_fingerprint() {
  local out_file="$1"
  local schemas_in="'${SCHEMA_NAME}', '${TERM_SCHEMA_NAME}', '${OPS_SCHEMA_NAME}'"
  {
    echo "=== SCHEMAS ==="
    psql1 "SELECT nspname FROM pg_namespace WHERE nspname IN (${schemas_in}) ORDER BY nspname;"

    echo "=== TABLES/VIEWS (information_schema.tables) ==="
    psql1 "
      SELECT table_schema, table_name, table_type
      FROM information_schema.tables
      WHERE table_schema IN (${schemas_in})
      ORDER BY table_schema, table_name;
    "

    echo "=== COLUMNS ==="
    psql1 "
      SELECT table_schema, table_name, column_name, ordinal_position, data_type, is_nullable, COALESCE(column_default, '')
      FROM information_schema.columns
      WHERE table_schema IN (${schemas_in})
      ORDER BY table_schema, table_name, ordinal_position;
    "

    echo "=== PRIMARY KEYS ==="
    psql1 "
      SELECT n.nspname, r.relname, c.conname, pg_get_constraintdef(c.oid)
      FROM pg_constraint c
      JOIN pg_class r ON r.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = r.relnamespace
      WHERE c.contype = 'p' AND n.nspname IN (${schemas_in})
      ORDER BY n.nspname, r.relname, c.conname;
    "

    echo "=== FOREIGN KEYS ==="
    psql1 "
      SELECT n.nspname, r.relname, c.conname, pg_get_constraintdef(c.oid), c.condeferrable, c.condeferred
      FROM pg_constraint c
      JOIN pg_class r ON r.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = r.relnamespace
      WHERE c.contype = 'f' AND n.nspname IN (${schemas_in})
      ORDER BY n.nspname, r.relname, c.conname;
    "

    echo "=== UNIQUE CONSTRAINTS ==="
    psql1 "
      SELECT n.nspname, r.relname, c.conname, pg_get_constraintdef(c.oid)
      FROM pg_constraint c
      JOIN pg_class r ON r.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = r.relnamespace
      WHERE c.contype = 'u' AND n.nspname IN (${schemas_in})
      ORDER BY n.nspname, r.relname, c.conname;
    "

    echo "=== CHECK CONSTRAINTS ==="
    psql1 "
      SELECT n.nspname, r.relname, c.conname, pg_get_constraintdef(c.oid)
      FROM pg_constraint c
      JOIN pg_class r ON r.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = r.relnamespace
      WHERE c.contype = 'c' AND n.nspname IN (${schemas_in})
      ORDER BY n.nspname, r.relname, c.conname;
    "

    echo "=== INDEXES ==="
    psql1 "
      SELECT schemaname, tablename, indexname, indexdef
      FROM pg_indexes
      WHERE schemaname IN (${schemas_in})
      ORDER BY schemaname, tablename, indexname;
    "

    echo "=== VIEWS ==="
    psql1 "
      SELECT schemaname, viewname, definition
      FROM pg_views
      WHERE schemaname IN (${schemas_in})
      ORDER BY schemaname, viewname;
    "

    echo "=== MATERIALIZED VIEWS ==="
    psql1 "
      SELECT schemaname, matviewname, definition
      FROM pg_matviews
      WHERE schemaname IN (${schemas_in})
      ORDER BY schemaname, matviewname;
    "

    echo "=== FUNCTIONS/PROCEDURES ==="
    psql1 "
      SELECT n.nspname, p.proname, pg_get_functiondef(p.oid)
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname IN (${schemas_in})
      ORDER BY n.nspname, p.proname, pg_get_functiondef(p.oid);
    "

    echo "=== TRIGGERS (excluding internal FK-enforcement triggers) ==="
    psql1 "
      SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE NOT t.tgisinternal AND n.nspname IN (${schemas_in})
      ORDER BY n.nspname, c.relname, t.tgname;
    "

    echo "=== MIGRATION-HISTORY TABLE COLUMNS ==="
    psql1 "
      SELECT column_name, ordinal_position, data_type, is_nullable, COALESCE(column_default, '')
      FROM information_schema.columns
      WHERE table_schema = '${OPS_SCHEMA_NAME}' AND table_name = 'schema_migrations'
      ORDER BY ordinal_position;
    "

    echo "=== MIGRATION-HISTORY TABLE CONSTRAINTS ==="
    psql1 "
      SELECT c.conname, c.contype, pg_get_constraintdef(c.oid)
      FROM pg_constraint c
      JOIN pg_class r ON r.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = r.relnamespace
      WHERE n.nspname = '${OPS_SCHEMA_NAME}' AND r.relname = 'schema_migrations'
      ORDER BY c.conname;
    "
  } > "$out_file"
}

# --- Focused foreign-key catalog (kept from the original version) ----------
declare -a EXPECTED_FKS=(
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

dump_fk_catalog() {
  local out_file="$1"
  psql1 "
    SELECT r.relname, c.conname, c.contype, c.condeferrable, c.condeferred, COUNT(*) OVER (PARTITION BY r.relname, c.conname)
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE n.nspname = '${SCHEMA_NAME}'
      AND c.contype = 'f';
  " > "$out_file"
}

check_fk_catalog() {
  local catalog_file="$1"
  local failures=0
  for pair in "${EXPECTED_FKS[@]}"; do
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

# --- 6) Validate the first run's history against the discovered set --------
echo "--- Validating migration history after the first run ---"
dump_history "$TMP_DIR/history_1.txt"

HISTORY_ROW_COUNT="$(wc -l < "$TMP_DIR/history_1.txt" | tr -d ' ')"
if [[ "$HISTORY_ROW_COUNT" -ne "$EXPECTED_COUNT" ]]; then
  fail "schema_migrations has ${HISTORY_ROW_COUNT} row(s) after the first run, expected exactly ${EXPECTED_COUNT} (one per discovered migration)"
fi

# No duplicate versions.
DUP_VERSIONS="$(cut -d'|' -f1 "$TMP_DIR/history_1.txt" | sort | uniq -d)"
if [[ -n "$DUP_VERSIONS" ]]; then
  fail "schema_migrations has duplicate version row(s) after the first run: ${DUP_VERSIONS}"
fi

# Every discovered migration has exactly one history row, with a matching
# checksum, execution mode, and execution_count == 1 (all real migrations
# are one_time; a one_time migration's first application always sets
# execution_count = 1 -- see src/tuva_postgres/migrations.py::_apply_one_time).
while IFS='|' read -r version execution checksum; do
  [[ -z "$version" ]] && continue
  row="$(awk -F'|' -v v="$version" '$1==v' "$TMP_DIR/history_1.txt")"
  if [[ -z "$row" ]]; then
    fail "discovered migration ${version} has no history row after the first run"
  fi
  history_checksum="$(echo "$row" | awk -F'|' '{print $3}')"
  history_execution="$(echo "$row" | awk -F'|' '{print $7}')"
  history_count="$(echo "$row" | awk -F'|' '{print $8}')"

  if [[ "$history_checksum" != "$checksum" ]]; then
    fail "migration ${version}: stored checksum '${history_checksum}' does not match the repository's computed checksum '${checksum}'"
  fi
  if [[ "$history_execution" != "$execution" ]]; then
    fail "migration ${version}: stored execution mode '${history_execution}' does not match its manifest's '${execution}'"
  fi
  if [[ "$execution" == "one_time" && "$history_count" != "1" ]]; then
    fail "migration ${version}: execution_count=${history_count} after its first (and only expected) application, expected 1"
  fi
done < "$TMP_DIR/expected_migrations.txt"
echo "Confirmed: ${HISTORY_ROW_COUNT} history row(s), one per discovered migration, no duplicates, checksums/execution modes/counts all correct."

# --- Migration status after the first run: fully current, no integrity issues
echo "--- Checking migration status after the first run ---"
set +e
run_migrations --status > "$TMP_DIR/status_after_first.out" 2>&1
STATUS_AFTER_FIRST_RC=$?
set -e
cat "$TMP_DIR/status_after_first.out"
[[ "$STATUS_AFTER_FIRST_RC" -eq 0 ]] || fail "migration status exited nonzero after the first run"
if ! grep -qE "^Pending one-time migrations \(0\):" "$TMP_DIR/status_after_first.out"; then
  fail "status after the first run reports pending one-time migration(s), expected 0"
fi
if ! grep -qE "^Repeatable migrations awaiting initial application \(0\):" "$TMP_DIR/status_after_first.out"; then
  fail "status after the first run reports repeatable migration(s) awaiting initial application, expected 0"
fi
if ! grep -qE "^Repeatable migrations awaiting reapplication, checksum changed \(0\):" "$TMP_DIR/status_after_first.out"; then
  fail "status after the first run reports repeatable migration(s) awaiting reapplication, expected 0"
fi
if grep -qE "^(ONE-TIME CHECKSUM MISMATCH|EXECUTION MODE MISMATCH):" "$TMP_DIR/status_after_first.out"; then
  fail "status after the first run reports a checksum or execution-mode mismatch"
fi
echo "Confirmed: status after the first run has zero pending work and no integrity failures."

# --- 6, cont.) Comprehensive catalog fingerprint (baseline, before 2nd run) -
echo "--- Capturing the complete catalog fingerprint before the second run ---"
dump_fk_catalog "$TMP_DIR/fk_catalog_1.txt"
FK_FAILURES_1="$(check_fk_catalog "$TMP_DIR/fk_catalog_1.txt")"
if [[ "$FK_FAILURES_1" -gt 0 ]]; then
  echo "----- FK catalog rows after first run -----" >&2
  cat "$TMP_DIR/fk_catalog_1.txt" >&2
  fail "${FK_FAILURES_1} focused foreign-key assertion(s) failed for schema ${SCHEMA_NAME} after the first migration run"
fi
echo "First run: all ${#EXPECTED_FKS[@]} expected foreign keys present exactly once, deferrable and initially deferred."

dump_full_fingerprint "$TMP_DIR/fingerprint_1.txt"
FINGERPRINT_LINES_1="$(wc -l < "$TMP_DIR/fingerprint_1.txt" | tr -d ' ')"
echo "Captured a ${FINGERPRINT_LINES_1}-line complete catalog fingerprint after the first run."

# --- 7) Second application: must be a true no-op ----------------------------
echo "--- Applying migrations (second run) via scripts/apply_schema.sh ---"
set +e
run_migrations > "$TMP_DIR/second_run.out" 2>&1
SECOND_STATUS=$?
set -e
cat "$TMP_DIR/second_run.out"
if [[ "$SECOND_STATUS" -ne 0 ]]; then
  fail "second migration run exited nonzero (status ${SECOND_STATUS}) -- re-running an already-applied migration set must succeed as a no-op"
fi
if ! grep -qF "No pending migrations. Database is up to date." "$TMP_DIR/second_run.out"; then
  fail "second migration run did not report zero pending migrations -- expected a true no-op re-run"
fi
if grep -qE "^Applied [0-9]" "$TMP_DIR/second_run.out"; then
  fail "second migration run reported applying a migration -- expected zero migrations applied"
fi
echo "Second run applied zero migrations (checksum-matched no-op)."

# --- History unchanged after the second run ---------------------------------
echo "--- Validating migration history is unchanged after the second run ---"
dump_history "$TMP_DIR/history_2.txt"
if ! diff -q "$TMP_DIR/history_1.txt" "$TMP_DIR/history_2.txt" > /dev/null; then
  echo "----- history before second run -----" >&2
  cat "$TMP_DIR/history_1.txt" >&2
  echo "----- history after second run -----" >&2
  cat "$TMP_DIR/history_2.txt" >&2
  fail "migration history changed between the first and second (no-op) migration run -- diff above"
fi
echo "Confirmed: full migration-history snapshot (version, description, checksum, applied_at,"
echo "           duration_ms, app_version, execution, execution_count) is byte-for-byte unchanged."

# --- Comprehensive catalog fingerprint unchanged after the second run ------
echo "--- Validating the complete catalog fingerprint is unchanged after the second run ---"
dump_fk_catalog "$TMP_DIR/fk_catalog_2.txt"
FK_FAILURES_2="$(check_fk_catalog "$TMP_DIR/fk_catalog_2.txt")"
if [[ "$FK_FAILURES_2" -gt 0 ]]; then
  echo "----- FK catalog rows after second run -----" >&2
  cat "$TMP_DIR/fk_catalog_2.txt" >&2
  fail "${FK_FAILURES_2} focused foreign-key assertion(s) failed for schema ${SCHEMA_NAME} after the second (no-op) migration run"
fi
if ! diff -q <(sort "$TMP_DIR/fk_catalog_1.txt") <(sort "$TMP_DIR/fk_catalog_2.txt") > /dev/null; then
  echo "----- FK catalog rows after first run -----" >&2
  cat "$TMP_DIR/fk_catalog_1.txt" >&2
  echo "----- FK catalog rows after second run -----" >&2
  cat "$TMP_DIR/fk_catalog_2.txt" >&2
  fail "focused foreign-key catalog changed between the first and second (no-op) migration run"
fi

dump_full_fingerprint "$TMP_DIR/fingerprint_2.txt"
if ! diff -q "$TMP_DIR/fingerprint_1.txt" "$TMP_DIR/fingerprint_2.txt" > /dev/null; then
  echo "----- complete catalog fingerprint diff (before vs. after second run) -----" >&2
  diff -u "$TMP_DIR/fingerprint_1.txt" "$TMP_DIR/fingerprint_2.txt" >&2 || true
  fail "the complete catalog fingerprint changed between the first and second (no-op) migration run -- diff above (a duplicate object, changed definition, or unexpected mutation was introduced by the second runner invocation)"
fi
echo "Confirmed: complete catalog fingerprint (schemas, tables, views, materialized views,"
echo "           columns, primary/foreign/unique/check constraints, indexes, functions,"
echo "           non-internal triggers, and the migration-history table's own structure)"
echo "           is byte-for-byte unchanged."

# --- 8) Final status: read-only, current, and does not mutate history ------
echo "--- Verifying final migration status is read-only and current ---"
dump_history "$TMP_DIR/history_before_status.txt"
set +e
run_migrations --status > "$TMP_DIR/final_status.out" 2>&1
FINAL_STATUS_RC=$?
set -e
cat "$TMP_DIR/final_status.out"
dump_history "$TMP_DIR/history_after_status.txt"

[[ "$FINAL_STATUS_RC" -eq 0 ]] || fail "final migration status exited nonzero"
if ! grep -qE "^Pending one-time migrations \(0\):" "$TMP_DIR/final_status.out"; then
  fail "final status reports pending one-time migration(s), expected 0"
fi
if ! grep -qE "^Repeatable migrations awaiting initial application \(0\):" "$TMP_DIR/final_status.out"; then
  fail "final status reports repeatable migration(s) awaiting initial application, expected 0"
fi
if ! grep -qE "^Repeatable migrations awaiting reapplication, checksum changed \(0\):" "$TMP_DIR/final_status.out"; then
  fail "final status reports repeatable migration(s) awaiting reapplication, expected 0"
fi
if grep -qE "^(ONE-TIME CHECKSUM MISMATCH|EXECUTION MODE MISMATCH):" "$TMP_DIR/final_status.out"; then
  fail "final status reports a checksum or execution-mode mismatch"
fi
if ! diff -q "$TMP_DIR/history_before_status.txt" "$TMP_DIR/history_after_status.txt" > /dev/null; then
  echo "----- history before --status -----" >&2
  cat "$TMP_DIR/history_before_status.txt" >&2
  echo "----- history after --status -----" >&2
  cat "$TMP_DIR/history_after_status.txt" >&2
  fail "migration history changed as a result of calling --status -- status must be strictly read-only"
fi
echo "Confirmed: final status is current (zero pending, no integrity failures) and read-only"
echo "           (migration history is unchanged before vs. after the --status call)."

echo ""
echo "PASS: scripts/apply_schema.sh applied all ${FIRST_APPLIED_COUNT} discovered migration(s) cleanly on"
echo "      the first run; the second, identical run applied zero migrations; the complete"
echo "      migration-history snapshot (all 8 columns, including applied_at/duration_ms/"
echo "      execution_count) was byte-for-byte unchanged; the complete deterministic catalog"
echo "      fingerprint (schemas/tables/views/columns/constraints/indexes/functions/triggers/"
echo "      migration-history structure) was byte-for-byte unchanged; all ${#EXPECTED_FKS[@]} expected"
echo "      foreign keys remained present exactly once, deferrable and initially deferred; and"
echo "      final migration status is current with zero pending work, no integrity failures,"
echo "      and did not mutate history."
