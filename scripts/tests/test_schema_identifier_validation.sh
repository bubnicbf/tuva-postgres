#!/usr/bin/env bash
# Regression test proving that every shell entry point taking a dynamic
# PostgreSQL schema name (PG_SCHEMA / TERMINOLOGY_SCHEMA) rejects an
# unsafe value through scripts/lib/postgres_identifiers.sh BEFORE ever
# invoking psql, and that scripts/apply_schema.sh (which delegates to the
# real Python migration runner, never psql directly) rejects an unsafe
# PG_SCHEMA before the migration runner's SQL execution layer is ever
# reached.
#
# No real PostgreSQL server is required:
#   - scripts/load_to_postgres.sh and scripts/run_tests.sh are exercised
#     against a `psql` STUB on PATH that records every invocation to a
#     log file and exits 0 -- if a hostile schema value ever reached
#     psql, it would show up verbatim in that log, so "the log is empty"
#     is direct proof psql was never invoked at all.
#   - scripts/apply_schema.sh is exercised against the REAL python3 (no
#     stub) with psycopg deliberately NOT required to be installed: since
#     this repository's db.py/migrations.py import psycopg lazily (only
#     inside functions that actually touch a connection), a hostile
#     PG_SCHEMA that reached PipelineConfig.load() but not the identifier
#     policy would surface as a *different* failure mode reaching for
#     psycopg -- so asserting the failure is the identifier-policy error,
#     and never mentions psycopg/connecting, is direct proof the SQL
#     execution layer was never reached either.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

STUB_BIN_DIR="$TMP_DIR/bin"
export PSQL_LOG="$TMP_DIR/psql_invocations.log"
mkdir -p "$STUB_BIN_DIR"
: > "$PSQL_LOG"

fail() {
  echo "FAIL: $1" >&2
  echo "----- recorded psql invocations -----" >&2
  cat "$PSQL_LOG" >&2
  echo "--------------------------------------" >&2
  exit 1
}

# --- psql stub: records every invocation (including any -f file's content,
#     since load_to_postgres.sh's TRUNCATE/COPY statements are passed via a
#     temp file, not inline -c text) and never touches a real database.
cat > "$STUB_BIN_DIR/psql" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PSQL_LOG"
file_arg=""
prev=""
for a in "$@"; do
  if [[ "$prev" == "-f" ]]; then
    file_arg="$a"
  fi
  prev="$a"
done
if [[ -n "$file_arg" && -f "$file_arg" ]]; then
  {
    echo "--- FILE CONTENT (${file_arg}) ---"
    cat "$file_arg"
    echo "--- END FILE CONTENT ---"
  } >> "$PSQL_LOG"
fi
exit 0
STUB
chmod +x "$STUB_BIN_DIR/psql"

# python3 stub for run_tests.sh's ingest_test_csv.py call, in case a test
# case reaches that far (it must not, for the hostile inputs below, but a
# stub is provided so any bug that lets it get further still fails
# cleanly on an assertion rather than hanging on a real network call).
cat > "$STUB_BIN_DIR/python3" <<'STUB'
#!/usr/bin/env bash
printf 'run_id,suite,test,pass,payload\n'
exit 0
STUB
chmod +x "$STUB_BIN_DIR/python3"

# Representative hostile identifiers covering every rejection class the
# shared policy (src/tuva_postgres/identifiers.py, mirrored in
# scripts/lib/postgres_identifiers.sh) must reject.
#
# Deliberately excludes the empty string: both load_to_postgres.sh
# (`${PG_SCHEMA:?PG_SCHEMA not set}`) and run_tests.sh
# (`${PG_SCHEMA:-tuva}`) treat an empty PG_SCHEMA the same as an unset
# one at the shell-parameter-expansion level, before the identifier
# validator ever runs -- an empty value is therefore already rejected
# (load_to_postgres.sh) or safely defaulted away (run_tests.sh) by a
# different, earlier mechanism than the one under test here. The shared
# policy's own empty-string rejection is covered directly in
# tests/unit/test_identifiers.py.
declare -a HOSTILE_IDENTIFIERS=(
  "tuva; DROP TABLE patient"
  "tuva--comment"
  "tuva/*comment*/"
  "tuva ops"
  "tuva.ops"
  'tuva"ops'
  "tuva'ops"
  $'tuva\nops'
  $'tuva\tops'
  "1tuva"
  "tuva-ops"
)

declare -a VALID_IDENTIFIERS=(
  "tuva"
  "tuva_ops"
  "_temporary"
  "TestSchema_1"
)

# --- 1) load_to_postgres.sh rejects every hostile PG_SCHEMA before psql ----
LOAD_DATA_DIR="$TMP_DIR/data"
mkdir -p "$LOAD_DATA_DIR"
# No CSVs present at all -- irrelevant either way, since a hostile
# PG_SCHEMA must be rejected before the loader even looks at DATA_DIR.

for hostile in "${HOSTILE_IDENTIFIERS[@]}"; do
  : > "$PSQL_LOG"
  set +e
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$hostile" \
    DATA_DIR="$LOAD_DATA_DIR" \
    bash "$REPO_ROOT/scripts/load_to_postgres.sh" > "$TMP_DIR/load.stdout" 2>&1
  STATUS=$?
  set -e
  if [[ $STATUS -eq 0 ]]; then
    fail "load_to_postgres.sh exited 0 for hostile PG_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if ! grep -qF "not a safe SQL identifier" "$TMP_DIR/load.stdout"; then
    fail "load_to_postgres.sh did not report a clear identifier-validation error for hostile PG_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if [[ -s "$PSQL_LOG" ]]; then
    fail "load_to_postgres.sh invoked psql for hostile PG_SCHEMA=$(printf '%q' "$hostile") -- psql must never be reached"
  fi
  if grep -qF "$hostile" "$PSQL_LOG" 2>/dev/null; then
    fail "the hostile PG_SCHEMA value itself appeared in a psql invocation log"
  fi
done

# --- 2) run_tests.sh rejects every hostile PG_SCHEMA / TERMINOLOGY_SCHEMA --
#        before psql, using a minimal db/tests/ fixture so the script has
#        somewhere valid to look if it (incorrectly) got past validation.
RUN_TESTS_SCRATCH="$TMP_DIR/run_tests_repo"
mkdir -p "$RUN_TESTS_SCRATCH/scripts/lib" "$RUN_TESTS_SCRATCH/db/tests"
cp "$REPO_ROOT/scripts/run_tests.sh" "$RUN_TESTS_SCRATCH/scripts/run_tests.sh"
cp "$REPO_ROOT/scripts/ingest_test_csv.py" "$RUN_TESTS_SCRATCH/scripts/ingest_test_csv.py"
cp "$REPO_ROOT/scripts/lib/postgres_identifiers.sh" "$RUN_TESTS_SCRATCH/scripts/lib/postgres_identifiers.sh"
cat > "$RUN_TESTS_SCRATCH/db/tests/zz_results.sql" <<'SQL'
SELECT 1;
SQL
cat > "$RUN_TESTS_SCRATCH/db/tests/a_smoke.sql" <<'SQL'
SELECT 'a_smoke' AS test, true AS pass;
SQL

for hostile in "${HOSTILE_IDENTIFIERS[@]}"; do
  : > "$PSQL_LOG"
  set +e
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$hostile" \
    TERMINOLOGY_SCHEMA="tuva_term" \
    bash "$RUN_TESTS_SCRATCH/scripts/run_tests.sh" > "$TMP_DIR/run_tests.stdout" 2>&1
  STATUS=$?
  set -e
  if [[ $STATUS -eq 0 ]]; then
    fail "run_tests.sh exited 0 for hostile PG_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if ! grep -qF "not a safe SQL identifier" "$TMP_DIR/run_tests.stdout"; then
    fail "run_tests.sh did not report a clear identifier-validation error for hostile PG_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if [[ -s "$PSQL_LOG" ]]; then
    fail "run_tests.sh invoked psql for hostile PG_SCHEMA=$(printf '%q' "$hostile") -- psql must never be reached"
  fi
done

for hostile in "${HOSTILE_IDENTIFIERS[@]}"; do
  : > "$PSQL_LOG"
  set +e
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="tuva" \
    TERMINOLOGY_SCHEMA="$hostile" \
    bash "$RUN_TESTS_SCRATCH/scripts/run_tests.sh" > "$TMP_DIR/run_tests.stdout" 2>&1
  STATUS=$?
  set -e
  if [[ $STATUS -eq 0 ]]; then
    fail "run_tests.sh exited 0 for hostile TERMINOLOGY_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if ! grep -qF "not a safe SQL identifier" "$TMP_DIR/run_tests.stdout"; then
    fail "run_tests.sh did not report a clear identifier-validation error for hostile TERMINOLOGY_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if [[ -s "$PSQL_LOG" ]]; then
    fail "run_tests.sh invoked psql for hostile TERMINOLOGY_SCHEMA=$(printf '%q' "$hostile") -- psql must never be reached"
  fi
done

# --- 3) valid PG_SCHEMA values are accepted and DO reach the psql stub -----
#        (proves the guard rejects hostile input specifically, not every
#        input -- a validator that rejected everything would trivially
#        "pass" the checks above for the wrong reason).
for valid in "${VALID_IDENTIFIERS[@]}"; do
  : > "$PSQL_LOG"
  set +e
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$valid" \
    TERMINOLOGY_SCHEMA="tuva_term" \
    bash "$RUN_TESTS_SCRATCH/scripts/run_tests.sh" > "$TMP_DIR/run_tests_valid.stdout" 2>&1
  STATUS=$?
  set -e
  if [[ $STATUS -ne 0 ]]; then
    fail "run_tests.sh rejected a VALID PG_SCHEMA=$(printf '%q' "$valid") -- $(cat "$TMP_DIR/run_tests_valid.stdout")"
  fi
  if [[ ! -s "$PSQL_LOG" ]]; then
    fail "run_tests.sh did not invoke psql at all for a valid PG_SCHEMA=$(printf '%q' "$valid")"
  fi
  if ! grep -qF "$valid" "$PSQL_LOG"; then
    fail "run_tests.sh's psql invocation did not reference the valid PG_SCHEMA=$(printf '%q' "$valid")"
  fi
done

# --- 4) load_to_postgres.sh's fixed managed-table names are still used, ---
#        unchanged, once a valid PG_SCHEMA is supplied (the identifier
#        guard must not have disturbed the separate, hardcoded table list).
declare -a MANAGED_TABLES=(
  "practitioner" "location" "patient" "encounter" "person_id_crosswalk"
  "medical_claim" "pharmacy_claim" "eligibility" "procedure" "observation"
  "lab_result" "condition" "medication" "immunization" "appointment"
)
VALID_LOAD_DATA_DIR="$TMP_DIR/valid_data"
mkdir -p "$VALID_LOAD_DATA_DIR"
for t in "${MANAGED_TABLES[@]}"; do
  printf 'id\n1\n' > "$VALID_LOAD_DATA_DIR/${t}.csv"
done

: > "$PSQL_LOG"
set +e
PATH="$STUB_BIN_DIR:$PATH" \
  PG_DSN="postgresql://test:test@localhost:5432/testdb" \
  PG_SCHEMA="tuva" \
  DATA_DIR="$VALID_LOAD_DATA_DIR" \
  bash "$REPO_ROOT/scripts/load_to_postgres.sh" > "$TMP_DIR/load_valid.stdout" 2>&1
STATUS=$?
set -e
if [[ $STATUS -ne 0 ]]; then
  fail "load_to_postgres.sh rejected a valid, complete snapshot with a valid PG_SCHEMA -- $(cat "$TMP_DIR/load_valid.stdout")"
fi
if ! grep -qF "CREATE SCHEMA IF NOT EXISTS \"tuva\"" "$PSQL_LOG"; then
  fail "load_to_postgres.sh did not create the configured schema \"tuva\""
fi
for t in "${MANAGED_TABLES[@]}"; do
  if ! grep -qF "\"tuva\".\"${t}\"" "$PSQL_LOG"; then
    fail "load_to_postgres.sh's generated SQL did not reference managed table ${t} under the valid schema"
  fi
done

# --- 5) apply_schema.sh (delegates to the REAL Python migration runner, ---
#        never psql) rejects a hostile PG_SCHEMA before the migration
#        runner's SQL execution layer is reached. Uses the REAL python3
#        (no stub) via the PYTHONPATH fallback path (no `uv` on PATH), so
#        this exercises actual production validation code -- but requires
#        no real PostgreSQL server and no psycopg installation: if the
#        hostile value slipped past the identifier policy, the very next
#        thing that would happen is `db.connect()` trying to import
#        psycopg, which would surface as a *different* error mentioning
#        psycopg -- so asserting the actual failure is the identifier
#        error, and never mentions psycopg, proves the SQL layer was
#        never reached.
NO_UV_BIN_DIR="$TMP_DIR/bin_no_uv"
mkdir -p "$NO_UV_BIN_DIR"
# A minimal, real python3 must be reachable -- copy the real one onto an
# otherwise-empty PATH entry so `command -v uv` in apply_schema.sh fails
# (forcing the PYTHONPATH fallback) while python3 itself still works.
REAL_PYTHON3="$(command -v python3)"
ln -s "$REAL_PYTHON3" "$NO_UV_BIN_DIR/python3"

for hostile in "tuva; DROP TABLE patient" 'tuva"ops' "tuva.ops" "tuva ops"; do
  set +e
  PATH="$NO_UV_BIN_DIR:/usr/bin:/bin" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$hostile" \
    bash "$REPO_ROOT/scripts/apply_schema.sh" --status > "$TMP_DIR/apply_schema.stdout" 2>&1
  STATUS=$?
  set -e
  if [[ $STATUS -eq 0 ]]; then
    fail "apply_schema.sh exited 0 for hostile PG_SCHEMA=$(printf '%q' "$hostile")"
  fi
  if ! grep -qF "PG_SCHEMA" "$TMP_DIR/apply_schema.stdout"; then
    fail "apply_schema.sh's failure for hostile PG_SCHEMA=$(printf '%q' "$hostile") did not mention PG_SCHEMA -- $(cat "$TMP_DIR/apply_schema.stdout")"
  fi
  if grep -qiF "psycopg" "$TMP_DIR/apply_schema.stdout"; then
    fail "apply_schema.sh's failure for hostile PG_SCHEMA=$(printf '%q' "$hostile") mentioned psycopg -- this means the hostile value reached the database-connection layer instead of being rejected by config/identifier validation first: $(cat "$TMP_DIR/apply_schema.stdout")"
  fi
done

echo "PASS: load_to_postgres.sh and run_tests.sh reject every hostile"
echo "      PG_SCHEMA/TERMINOLOGY_SCHEMA value (semicolons, comments,"
echo "      whitespace, dots, quotes, newlines, tabs, leading digits, hyphens)"
echo "      before ever invoking psql, accept valid identifiers and pass them"
echo "      through to psql unchanged, and apply_schema.sh rejects a hostile"
echo "      PG_SCHEMA before the real migration runner's SQL execution layer"
echo "      (psycopg/database connection) is ever reached."
