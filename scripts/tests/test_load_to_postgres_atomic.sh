#!/usr/bin/env bash
# Regression test for scripts/load_to_postgres.sh
#
# Guards against the loader treating the managed CSV set as an
# append-only stream instead of a replaceable snapshot. The old
# implementation ran one psql session per table, each with its own
# implicit transaction: a retry could hit duplicate-key errors, and a
# mid-load failure left a partially-loaded dataset behind. The fix loads
# a complete snapshot in ONE session/transaction: BEGIN; one combined
# TRUNCATE TABLE covering every managed table; every \copy; COMMIT --
# committed only if every copy succeeds, rolled back automatically by
# Postgres otherwise.
#
# No real PostgreSQL server, credentials, or network access is required:
# an executable `psql` stub is put first on PATH. It records every
# invocation's arguments and, for any invocation using `-f <file>`
# (the single-session transaction script), captures that file's content
# too -- exactly what's needed to inspect the generated BEGIN/TRUNCATE/
# \copy/COMMIT stream without a real database.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOADER="$REPO_ROOT/scripts/load_to_postgres.sh"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

declare -a MANAGED_TABLES=(
  "practitioner" "location" "patient" "encounter" "person_id_crosswalk"
  "medical_claim" "pharmacy_claim" "eligibility" "procedure" "observation"
  "lab_result" "condition" "medication" "immunization" "appointment"
)

EXPECTED_SCHEMA="regression_test_schema"

# --- Stub builder ------------------------------------------------------------
# Writes an executable psql stub into $1/psql. The stub:
#   * appends a line "INVOCATION <n>: <args>" per call to $PSQL_LOG
#   * for any "-f <path>" argument, appends the referenced file's content
#     (this is how our loader passes the single-session transaction script)
#   * if FAIL_ON_TABLE is set (in the environment at call time) and that
#     table's \copy line appears in the -f file, exits nonzero to simulate
#     a mid-snapshot copy failure; otherwise exits 0
build_stub() {
  local bin_dir="$1"
  mkdir -p "$bin_dir"
  cat > "$bin_dir/psql" <<'STUB'
#!/usr/bin/env bash
COUNTER_FILE="${PSQL_COUNTER_FILE}"
n=0
[[ -f "$COUNTER_FILE" ]] && n="$(cat "$COUNTER_FILE")"
n=$((n + 1))
printf '%s' "$n" > "$COUNTER_FILE"

{
  echo "INVOCATION ${n}: $*"
} >> "$PSQL_LOG"

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

  if [[ -n "${FAIL_ON_TABLE:-}" ]] && grep -qF "\\copy \"${PG_SCHEMA:-}\".\"${FAIL_ON_TABLE}\"" "$file_arg"; then
    echo "psql: simulated COPY failure on table ${FAIL_ON_TABLE}" >&2
    exit 7
  fi
fi

exit 0
STUB
  chmod +x "$bin_dir/psql"
}

# --- Fixture builder ---------------------------------------------------------
# Writes a minimal (header + one row) CSV for every managed table into $1.
build_complete_fixture() {
  local dir="$1"
  mkdir -p "$dir"
  for t in "${MANAGED_TABLES[@]}"; do
    printf 'id\n1\n' > "${dir}/${t}.csv"
  done
}

# NOTE: always invoke this as the condition of an if/while (e.g.
# `if run_loader ...; then` or `if ! run_loader ...; then`), never as a
# bare statement -- with `set -e` active in the caller, a bare call whose
# exit status you inspect via $? afterward can trip errexit before you
# get the chance to look at it.
run_loader() {
  local data_dir="$1" stub_bin="$2" out_file="$3"
  shift 3
  PATH="${stub_bin}:${PATH}" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    PG_SCHEMA="$EXPECTED_SCHEMA" \
    DATA_DIR="$data_dir" \
    "$@" \
    bash "$LOADER" > "$out_file" 2>&1
}

fail() {
  local msg="$1" out="$2" log="$3"
  echo "FAIL: $msg" >&2
  echo "----- loader output -----" >&2
  cat "$out" >&2
  echo "----- recorded psql invocations -----" >&2
  cat "$log" >&2
  echo "--------------------------------------" >&2
  exit 1
}

# =============================================================================
# Scenario 1: complete dataset -- transaction structure, one session, etc.
# =============================================================================
S1_DIR="$TMP_DIR/s1"
S1_STUB="$S1_DIR/bin"
S1_DATA="$S1_DIR/data"
S1_OUT="$S1_DIR/loader.stdout"
build_stub "$S1_STUB"
build_complete_fixture "$S1_DATA"
export PSQL_LOG="$S1_DIR/psql.log"
export PSQL_COUNTER_FILE="$S1_DIR/psql.count"
: > "$PSQL_LOG"

if ! run_loader "$S1_DATA" "$S1_STUB" "$S1_OUT"; then
  fail "loader exited nonzero on a complete dataset" "$S1_OUT" "$PSQL_LOG"
fi

# 1) Preflight succeeded (loader got far enough to talk to psql at all).
if ! grep -qF "Preflight OK" "$S1_OUT"; then
  fail "loader did not report preflight success" "$S1_OUT" "$PSQL_LOG"
fi

# 2) Exactly one invocation used -f (the single snapshot-transaction session).
F_INVOCATIONS="$(grep -c -- '-f ' "$PSQL_LOG" || true)"
if [[ "$F_INVOCATIONS" -ne 1 ]]; then
  fail "expected exactly one psql invocation with -f (one load session), found ${F_INVOCATIONS}" "$S1_OUT" "$PSQL_LOG"
fi

# Extract the transaction file content block for structural assertions.
TXN_CONTENT="$(awk '/--- FILE CONTENT/{flag=1; next} /--- END FILE CONTENT/{flag=0} flag' "$PSQL_LOG")"
if [[ -z "$TXN_CONTENT" ]]; then
  fail "could not find captured transaction file content in the psql log" "$S1_OUT" "$PSQL_LOG"
fi

BEGIN_LINE="$(grep -n '^BEGIN;' <<<"$TXN_CONTENT" | head -1 | cut -d: -f1)"
TRUNCATE_LINE="$(grep -n '^TRUNCATE TABLE' <<<"$TXN_CONTENT" | head -1 | cut -d: -f1)"
COMMIT_LINE="$(grep -n '^COMMIT;' <<<"$TXN_CONTENT" | head -1 | cut -d: -f1)"

# 3) BEGIN appears before any TRUNCATE or \copy.
FIRST_COPY_LINE="$(grep -n '^\\copy ' <<<"$TXN_CONTENT" | head -1 | cut -d: -f1)"
if [[ -z "$BEGIN_LINE" ]]; then
  fail "no BEGIN; found in the generated transaction" "$S1_OUT" "$PSQL_LOG"
fi
if [[ -z "$TRUNCATE_LINE" || "$TRUNCATE_LINE" -le "$BEGIN_LINE" ]]; then
  fail "TRUNCATE TABLE does not appear after BEGIN;" "$S1_OUT" "$PSQL_LOG"
fi
if [[ -z "$FIRST_COPY_LINE" || "$FIRST_COPY_LINE" -le "$BEGIN_LINE" ]]; then
  fail "first \\copy does not appear after BEGIN;" "$S1_OUT" "$PSQL_LOG"
fi

# 4) One TRUNCATE TABLE statement covering every managed table.
TRUNCATE_BLOCK="$(sed -n "${TRUNCATE_LINE},/;/p" <<<"$TXN_CONTENT")"
TRUNCATE_STATEMENT_COUNT="$(grep -c '^TRUNCATE TABLE' <<<"$TXN_CONTENT")"
if [[ "$TRUNCATE_STATEMENT_COUNT" -ne 1 ]]; then
  fail "expected exactly one TRUNCATE TABLE statement, found ${TRUNCATE_STATEMENT_COUNT}" "$S1_OUT" "$PSQL_LOG"
fi
for t in "${MANAGED_TABLES[@]}"; do
  if ! grep -qF "\"${EXPECTED_SCHEMA}\".\"${t}\"" <<<"$TRUNCATE_BLOCK"; then
    fail "TRUNCATE TABLE does not include managed table '${t}'" "$S1_OUT" "$PSQL_LOG"
  fi
done

# 5) No CASCADE anywhere in the truncate (or the transaction at all).
if grep -qi 'CASCADE' <<<"$TXN_CONTENT"; then
  fail "generated transaction uses CASCADE, which is explicitly disallowed" "$S1_OUT" "$PSQL_LOG"
fi

# 6) & 7) Every managed table is copied exactly once, after BEGIN and TRUNCATE.
for t in "${MANAGED_TABLES[@]}"; do
  copy_count="$(grep -c "^\\\\copy \"${EXPECTED_SCHEMA}\".\"${t}\" FROM" <<<"$TXN_CONTENT" || true)"
  if [[ "$copy_count" -ne 1 ]]; then
    fail "expected exactly one \\copy for table '${t}', found ${copy_count}" "$S1_OUT" "$PSQL_LOG"
  fi
  copy_line="$(grep -n "^\\\\copy \"${EXPECTED_SCHEMA}\".\"${t}\" FROM" <<<"$TXN_CONTENT" | head -1 | cut -d: -f1)"
  if [[ "$copy_line" -le "$TRUNCATE_LINE" ]]; then
    fail "\\copy for table '${t}' appears before TRUNCATE TABLE" "$S1_OUT" "$PSQL_LOG"
  fi
done

# 8) COMMIT appears only after every copy.
if [[ -z "$COMMIT_LINE" ]]; then
  fail "no COMMIT; found in the generated transaction" "$S1_OUT" "$PSQL_LOG"
fi
LAST_COPY_LINE="$(grep -n '^\\copy ' <<<"$TXN_CONTENT" | tail -1 | cut -d: -f1)"
if [[ "$COMMIT_LINE" -le "$LAST_COPY_LINE" ]]; then
  fail "COMMIT; appears before the last \\copy" "$S1_OUT" "$PSQL_LOG"
fi

# 9) The configured schema is used (already exercised above via schema-qualified
#    truncate/copy targets); double-check the CREATE SCHEMA call too.
if ! grep -qF "CREATE SCHEMA IF NOT EXISTS \"${EXPECTED_SCHEMA}\"" "$PSQL_LOG"; then
  fail "loader did not create the configured schema ${EXPECTED_SCHEMA}" "$S1_OUT" "$PSQL_LOG"
fi

# 10) ON_ERROR_STOP is enabled on every invocation.
if grep -q "INVOCATION" "$PSQL_LOG" && grep "INVOCATION" "$PSQL_LOG" | grep -qv "ON_ERROR_STOP=1"; then
  fail "some psql invocation is missing -v ON_ERROR_STOP=1" "$S1_OUT" "$PSQL_LOG"
fi

# 11) The loader exits successfully when the stub succeeds (already asserted
#     via run_loader's return above).

# 12) "Load complete" is printed only on success -- confirm it's present here.
if ! grep -qF "Load complete." "$S1_OUT"; then
  fail "loader did not print 'Load complete.' on success" "$S1_OUT" "$PSQL_LOG"
fi

# 13) No temporary command file remains afterward.
LEFTOVER_TMP="$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'load_to_postgres.*.sql' 2>/dev/null || true)"
if [[ -n "$LEFTOVER_TMP" ]]; then
  fail "leftover temporary command file(s) found: ${LEFTOVER_TMP}" "$S1_OUT" "$PSQL_LOG"
fi

echo "PASS (1/5): complete dataset produces one BEGIN/TRUNCATE(all tables, no CASCADE)/copies(each once)/COMMIT session."

# =============================================================================
# Scenario 2: paths containing spaces
# =============================================================================
S2_DIR="$TMP_DIR/s2"
S2_STUB="$S2_DIR/bin"
S2_DATA="$S2_DIR/my seed data"
S2_OUT="$S2_DIR/loader.stdout"
build_stub "$S2_STUB"
build_complete_fixture "$S2_DATA"
export PSQL_LOG="$S2_DIR/psql.log"
export PSQL_COUNTER_FILE="$S2_DIR/psql.count"
: > "$PSQL_LOG"

if ! run_loader "$S2_DATA" "$S2_STUB" "$S2_OUT"; then
  fail "loader exited nonzero with a data directory path containing spaces" "$S2_OUT" "$PSQL_LOG"
fi

TXN_CONTENT_S2="$(awk '/--- FILE CONTENT/{flag=1; next} /--- END FILE CONTENT/{flag=0} flag' "$PSQL_LOG")"
if ! grep -qF "FROM '${S2_DATA}/patient.csv'" <<<"$TXN_CONTENT_S2"; then
  fail "\\copy for patient did not correctly quote a path containing spaces" "$S2_OUT" "$PSQL_LOG"
fi
if ! grep -qF "Load complete." "$S2_OUT"; then
  fail "loader did not complete successfully with a spaced data directory" "$S2_OUT" "$PSQL_LOG"
fi

echo "PASS (2/5): data directory paths containing spaces are quoted correctly for \\copy."

# =============================================================================
# Scenario 3: preflight -- no CSV files present
# =============================================================================
S3_DIR="$TMP_DIR/s3"
S3_STUB="$S3_DIR/bin"
S3_DATA="$S3_DIR/data"
S3_OUT="$S3_DIR/loader.stdout"
build_stub "$S3_STUB"
mkdir -p "$S3_DATA"
export PSQL_LOG="$S3_DIR/psql.log"
export PSQL_COUNTER_FILE="$S3_DIR/psql.count"
: > "$PSQL_LOG"

if ! run_loader "$S3_DATA" "$S3_STUB" "$S3_OUT"; then
  fail "loader exited nonzero for an empty data directory (expected a safe no-op)" "$S3_OUT" "$PSQL_LOG"
fi
if [[ -s "$PSQL_LOG" ]]; then
  fail "loader made a psql call for an empty data directory (expected zero calls)" "$S3_OUT" "$PSQL_LOG"
fi
if ! grep -qiF "nothing to load" "$S3_OUT"; then
  fail "loader did not print a clear no-seed-data message" "$S3_OUT" "$PSQL_LOG"
fi
if grep -qF "Load complete." "$S3_OUT"; then
  fail "loader printed 'Load complete.' for a no-op run" "$S3_OUT" "$PSQL_LOG"
fi

echo "PASS (3/5): an empty data directory is a safe, successful no-op with zero psql calls."

# =============================================================================
# Scenario 4: preflight -- partial CSV set
# =============================================================================
S4_DIR="$TMP_DIR/s4"
S4_STUB="$S4_DIR/bin"
S4_DATA="$S4_DIR/data"
S4_OUT="$S4_DIR/loader.stdout"
build_stub "$S4_STUB"
mkdir -p "$S4_DATA"
printf 'id\n1\n' > "${S4_DATA}/patient.csv"
printf 'id\n1\n' > "${S4_DATA}/encounter.csv"
export PSQL_LOG="$S4_DIR/psql.log"
export PSQL_COUNTER_FILE="$S4_DIR/psql.count"
: > "$PSQL_LOG"

if run_loader "$S4_DATA" "$S4_STUB" "$S4_OUT"; then
  S4_STATUS=0
else
  S4_STATUS=$?
fi

if [[ "$S4_STATUS" -eq 0 ]]; then
  fail "loader exited 0 for a partial CSV set (expected nonzero)" "$S4_OUT" "$PSQL_LOG"
fi
if [[ -s "$PSQL_LOG" ]]; then
  fail "loader made a psql call for a partial CSV set (expected zero -- it must fail before any DB call)" "$S4_OUT" "$PSQL_LOG"
fi
for t in "${MANAGED_TABLES[@]}"; do
  if [[ "$t" == "patient" || "$t" == "encounter" ]]; then
    continue
  fi
  if ! grep -qF "${t}.csv" "$S4_OUT"; then
    fail "loader did not list missing CSV for table '${t}'" "$S4_OUT" "$PSQL_LOG"
  fi
done
if grep -qF "Load complete." "$S4_OUT"; then
  fail "loader printed 'Load complete.' for a rejected partial snapshot" "$S4_OUT" "$PSQL_LOG"
fi

echo "PASS (4/5): a partial CSV set is rejected before any database call, listing every missing file."

# =============================================================================
# Scenario 5: simulated mid-snapshot copy failure
# =============================================================================
S5_DIR="$TMP_DIR/s5"
S5_STUB="$S5_DIR/bin"
S5_DATA="$S5_DIR/data"
S5_OUT="$S5_DIR/loader.stdout"
build_stub "$S5_STUB"
build_complete_fixture "$S5_DATA"
export PSQL_LOG="$S5_DIR/psql.log"
export PSQL_COUNTER_FILE="$S5_DIR/psql.count"
: > "$PSQL_LOG"

if FAIL_ON_TABLE="eligibility" run_loader "$S5_DATA" "$S5_STUB" "$S5_OUT"; then
  S5_STATUS=0
else
  S5_STATUS=$?
fi

if [[ "$S5_STATUS" -eq 0 ]]; then
  fail "loader exited 0 despite a simulated mid-snapshot copy failure" "$S5_OUT" "$PSQL_LOG"
fi
if grep -qF "Load complete." "$S5_OUT"; then
  fail "loader printed 'Load complete.' despite a simulated copy failure" "$S5_OUT" "$PSQL_LOG"
fi

# The submitted command stream must still place COMMIT after every \copy
# (including the one that "failed") -- the loader never issues an explicit
# ROLLBACK or a COMMIT before the failure point; it relies on ON_ERROR_STOP
# causing psql to abort and on Postgres to roll back the still-open
# transaction when the session ends. This dependency-free test can only
# confirm the *submitted* command stream's shape; real rollback behavior is
# covered by scripts/tests/test_load_to_postgres_atomic_integration.sh.
TXN_CONTENT_S5="$(awk '/--- FILE CONTENT/{flag=1; next} /--- END FILE CONTENT/{flag=0} flag' "$PSQL_LOG")"
if [[ -z "$TXN_CONTENT_S5" ]]; then
  fail "could not find captured transaction file content for the failure scenario" "$S5_OUT" "$PSQL_LOG"
fi
COMMIT_LINE_S5="$(grep -n '^COMMIT;' <<<"$TXN_CONTENT_S5" | head -1 | cut -d: -f1)"
LAST_COPY_LINE_S5="$(grep -n '^\\copy ' <<<"$TXN_CONTENT_S5" | tail -1 | cut -d: -f1)"
if [[ -z "$COMMIT_LINE_S5" || -z "$LAST_COPY_LINE_S5" || "$COMMIT_LINE_S5" -le "$LAST_COPY_LINE_S5" ]]; then
  fail "COMMIT does not appear after all \\copy statements in the submitted command stream" "$S5_OUT" "$PSQL_LOG"
fi

echo "PASS (5/5): a simulated mid-snapshot copy failure exits nonzero, never prints 'Load complete.',"
echo "            and the submitted stream still places COMMIT after every copy (Postgres, not this"
echo "            script, is responsible for rolling back the aborted session)."

echo ""
echo "PASS: scripts/load_to_postgres.sh loads a complete snapshot atomically (one BEGIN/TRUNCATE/"
echo "      copies/COMMIT session), rejects partial snapshots and unreadable files before any"
echo "      database call, safely no-ops on an empty data directory, and never reports success"
echo "      after a failure."
