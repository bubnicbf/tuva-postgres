#!/usr/bin/env bash
# Database-free regression test for the CONTROL FLOW of
# scripts/tests/test_schema_constraint_idempotency.sh -- the real
# migration-idempotency harness that CI runs against a disposable
# Postgres (see `make test-schema-idempotency`).
#
# This does NOT require Postgres and does NOT attempt to simulate real
# PostgreSQL catalog semantics. Instead it copies the real, unmodified
# harness script into a throwaway fake repo, points it at a tiny fake
# migration set (so the real discover()/compute_checksum() machinery
# still runs for real), and replaces only its two points of contact with
# the outside world -- `scripts/apply_schema.sh` and `psql` -- with
# small scenario-driven stubs on PATH. Each scenario answers one
# question: does the *harness itself* correctly pass a truly idempotent
# rerun and correctly fail every way a rerun can go wrong? The stubs
# never model real Postgres catalog behavior (constraint definitions,
# index rendering, trigger internals, etc.) -- that would just be a
# second, worse copy of Postgres. They only model the small set of
# externally observable outcomes the harness actually branches on:
# stdout text, exit codes, and the content of a few flat "state" files
# standing in for the migration history table and a catalog fingerprint.
#
# Convention: stdlib/coreutils-only, like the rest of scripts/tests/*.sh
# that don't require a database (see test_migration_execution_modes.sh
# for the analogous "prove the manifests are correct without a DB"
# counterpart -- this file proves the *harness script* is correct
# without a DB; the two are deliberately not duplicating each other).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REAL_HARNESS="$REPO_ROOT/scripts/tests/test_schema_constraint_idempotency.sh"

if [[ ! -f "$REAL_HARNESS" ]]; then
  echo "FAIL: expected to find $REAL_HARNESS" >&2
  exit 1
fi

FAKE_REPO="$(mktemp -d)"
cleanup() { rm -rf "$FAKE_REPO"; }
trap cleanup EXIT

FAILURES=0
fail_case() {
  echo "FAIL: $1" >&2
  FAILURES=$((FAILURES + 1))
}

# --- Build a minimal, valid fake repo the copied harness can run against
mkdir -p "$FAKE_REPO/scripts/tests"
mkdir -p "$FAKE_REPO/scripts/lib"
mkdir -p "$FAKE_REPO/bin"
mkdir -p "$FAKE_REPO/db/migrations/sql/0001_fake/core"
mkdir -p "$FAKE_REPO/db/migrations/sql/0002_fake/core"

# The real package, via a symlink -- run_python()'s PYTHONPATH fallback
# needs a real tuva_postgres.migrations module to discover()/compute
# checksums against the fake manifests below. No uv.lock is created, so
# run_python() always takes the plain-python3 branch here (deterministic,
# no dependency on uv being installed in this sandbox).
ln -s "$REPO_ROOT/src" "$FAKE_REPO/src"

cat > "$FAKE_REPO/db/migrations/0001_fake.json" << 'EOF'
{
  "version": "0001",
  "description": "fake baseline migration (harness control-flow test fixture)",
  "execution": "one_time",
  "vars": {},
  "files": ["db/migrations/sql/0001_fake/core/0001_fake.sql"]
}
EOF
echo "-- fake baseline migration, never executed against a real database" \
  > "$FAKE_REPO/db/migrations/sql/0001_fake/core/0001_fake.sql"

cat > "$FAKE_REPO/db/migrations/0002_fake.json" << 'EOF'
{
  "version": "0002",
  "description": "fake operational migration (harness control-flow test fixture)",
  "execution": "one_time",
  "vars": {},
  "files": ["db/migrations/sql/0002_fake/core/0002_fake.sql"]
}
EOF
echo "-- fake operational migration, never executed against a real database" \
  > "$FAKE_REPO/db/migrations/sql/0002_fake/core/0002_fake.sql"

# Copy the real, UNMODIFIED harness script -- this test exercises the
# actual production control flow, not a paraphrase of it.
cp "$REAL_HARNESS" "$FAKE_REPO/scripts/tests/test_schema_constraint_idempotency.sh"
cp "$REPO_ROOT/scripts/lib/postgres_identifiers.sh" "$FAKE_REPO/scripts/lib/postgres_identifiers.sh"

# apply_schema.sh is invoked by the harness via an explicit
# "$REPO_ROOT/scripts/apply_schema.sh" path, so a stub placed at that
# exact path is all that's needed -- no PATH lookup involved.
APPLY_STUB="$FAKE_REPO/scripts/apply_schema.sh"

# Extract the harness's real EXPECTED_FKS list so the fake FK catalog
# always matches whatever the production harness currently expects,
# instead of a second hardcoded copy that could silently drift out of
# sync with it.
FK_PAIRS_FILE="$FAKE_REPO/.fk_pairs.txt"
awk '
  /declare -a EXPECTED_FKS=\(/ { inarr = 1; next }
  inarr && /^\)/ { inarr = 0 }
  inarr {
    line = $0
    gsub(/^[ \t]+"/, "", line)
    gsub(/"[ \t]*$/, "", line)
    print line
  }
' "$REAL_HARNESS" > "$FK_PAIRS_FILE"

if [[ ! -s "$FK_PAIRS_FILE" ]]; then
  echo "FAIL: could not extract EXPECTED_FKS from $REAL_HARNESS -- has its format changed?" >&2
  exit 1
fi

# --- Compute the real checksums for the two fake migrations, using the
#     real compute_checksum(), so the stub's canned history rows are
#     genuinely correct (not just plausible-looking).
CHECKSUM_0001="$(PYTHONPATH="$REPO_ROOT/src" python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}/src')
from tuva_postgres.migrations import discover, compute_checksum
from pathlib import Path
for m in discover(Path('${FAKE_REPO}/db/migrations'), Path('${FAKE_REPO}')):
    if m.version == '0001':
        print(compute_checksum(m))
")"
CHECKSUM_0002="$(PYTHONPATH="$REPO_ROOT/src" python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}/src')
from tuva_postgres.migrations import discover, compute_checksum
from pathlib import Path
for m in discover(Path('${FAKE_REPO}/db/migrations'), Path('${FAKE_REPO}')):
    if m.version == '0002':
        print(compute_checksum(m))
")"

if [[ -z "$CHECKSUM_0001" || -z "$CHECKSUM_0002" ]]; then
  echo "FAIL: could not compute checksums for the fake migrations" >&2
  exit 1
fi

# --- The apply_schema.sh stub: a tiny, scenario-driven fake of "the real
#     migration runner", state-tracked via flat files under
#     $HARNESS_STUB_STATE_DIR (set per-scenario below).
cat > "$APPLY_STUB" << APPLYSTUB_EOF
#!/usr/bin/env bash
set -euo pipefail
STATE_DIR="\${HARNESS_STUB_STATE_DIR:?HARNESS_STUB_STATE_DIR not set}"
SCENARIO="\${HARNESS_STUB_SCENARIO:-good}"
CHECKSUM_0001="$CHECKSUM_0001"
CHECKSUM_0002="$CHECKSUM_0002"

status_call_n=0
if [[ -f "\$STATE_DIR/status_call_count" ]]; then
  status_call_n="\$(cat "\$STATE_DIR/status_call_count")"
fi

if [[ "\${1:-}" == "--status" ]]; then
  status_call_n=\$((status_call_n + 1))
  echo "\$status_call_n" > "\$STATE_DIR/status_call_count"

  if [[ "\$SCENARIO" == "final_status_pending" && "\$status_call_n" -eq 2 ]]; then
    echo "Applied one-time migrations (2):"
    echo "  0001: fake baseline migration (harness control-flow test fixture)"
    echo "  0002: fake operational migration (harness control-flow test fixture)"
    echo "Current repeatable migrations (0):"
    echo "Pending one-time migrations (1):"
    echo "  0003: a migration that was never actually applied"
    echo "Repeatable migrations awaiting initial application (0):"
    echo "Repeatable migrations awaiting reapplication, checksum changed (0):"
    exit 0
  fi

  echo "Applied one-time migrations (2):"
  echo "  0001: fake baseline migration (harness control-flow test fixture)"
  echo "  0002: fake operational migration (harness control-flow test fixture)"
  echo "Current repeatable migrations (0):"
  echo "Pending one-time migrations (0):"
  echo "Repeatable migrations awaiting initial application (0):"
  echo "Repeatable migrations awaiting reapplication, checksum changed (0):"
  exit 0
fi

phase=0
if [[ -f "\$STATE_DIR/phase" ]]; then
  phase="\$(cat "\$STATE_DIR/phase")"
fi

write_history() {
  {
    echo "0001|fake baseline migration (harness control-flow test fixture)|\$CHECKSUM_0001|2026-01-01T00:00:00+00:00|1.0|test|one_time|1"
    echo "0002|fake operational migration (harness control-flow test fixture)|\$CHECKSUM_0002|2026-01-01T00:00:01+00:00|1.0|test|one_time|1"
  } > "\$STATE_DIR/history.txt"
}

if [[ "\$phase" -eq 0 ]]; then
  if [[ "\$SCENARIO" == "first_run_applies_nothing" ]]; then
    echo "No pending migrations. Database is up to date."
    echo 1 > "\$STATE_DIR/phase"
    exit 0
  fi

  write_history
  touch "\$STATE_DIR/schemas_exist"
  echo "STUB-FINGERPRINT-V1" > "\$STATE_DIR/fingerprint.txt"
  cp "\$STATE_DIR/fk_catalog_baseline.txt" "\$STATE_DIR/fk_catalog.txt"
  echo "Applied 2 migration(s)."
  echo 1 > "\$STATE_DIR/phase"
  exit 0
fi

# phase >= 1: this is the second (expected no-op) apply invocation.
case "\$SCENARIO" in
  good)
    echo "No pending migrations. Database is up to date."
    ;;
  second_run_applies)
    write_history
    echo "0003|an accidentally-reapplied migration|deadbeef|2026-01-01T00:00:02+00:00|1.0|test|one_time|1" >> "\$STATE_DIR/history.txt"
    echo "Applied 1 migration(s)."
    ;;
  second_run_wrong_message)
    echo "Done, I guess."
    ;;
  second_run_nonzero_exit)
    echo "unexpected error" >&2
    exit 1
    ;;
  history_drifts)
    echo "0001|fake baseline migration (harness control-flow test fixture)|\$CHECKSUM_0001|2026-01-01T00:00:00+00:00|999.0|test|one_time|1" > "\$STATE_DIR/history.txt"
    echo "0002|fake operational migration (harness control-flow test fixture)|\$CHECKSUM_0002|2026-01-01T00:00:01+00:00|1.0|test|one_time|1" >> "\$STATE_DIR/history.txt"
    echo "No pending migrations. Database is up to date."
    ;;
  execution_count_drifts)
    echo "0001|fake baseline migration (harness control-flow test fixture)|\$CHECKSUM_0001|2026-01-01T00:00:00+00:00|1.0|test|one_time|2" > "\$STATE_DIR/history.txt"
    echo "0002|fake operational migration (harness control-flow test fixture)|\$CHECKSUM_0002|2026-01-01T00:00:01+00:00|1.0|test|one_time|1" >> "\$STATE_DIR/history.txt"
    echo "No pending migrations. Database is up to date."
    ;;
  fingerprint_drifts)
    echo "STUB-FINGERPRINT-V2-DRIFTED" > "\$STATE_DIR/fingerprint.txt"
    echo "No pending migrations. Database is up to date."
    ;;
  fk_drifts)
    tail -n +2 "\$STATE_DIR/fk_catalog_baseline.txt" > "\$STATE_DIR/fk_catalog.txt"
    echo "No pending migrations. Database is up to date."
    ;;
  final_status_pending)
    echo "No pending migrations. Database is up to date."
    ;;
  *)
    echo "No pending migrations. Database is up to date."
    ;;
esac
echo 2 > "\$STATE_DIR/phase"
exit 0
APPLYSTUB_EOF
chmod +x "$APPLY_STUB"

# --- The psql stub: recognizes the harness's queries by shape (never by
#     simulating what Postgres would actually compute) and answers from
#     the same flat state files the apply_schema.sh stub writes.
cat > "$FAKE_REPO/bin/psql" << 'PSQL_EOF'
#!/usr/bin/env bash
set -euo pipefail
STATE_DIR="${HARNESS_STUB_STATE_DIR:?HARNESS_STUB_STATE_DIR not set}"

SQL=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-c" ]]; then
    SQL="$arg"
  fi
  prev="$arg"
done

case "$SQL" in
  *"DROP SCHEMA"*)
    exit 0 ;;
  *"SELECT version()"*)
    echo "PostgreSQL 16.0 (harness control-flow stub)"
    exit 0 ;;
esac

# Note: matched WITHOUT a "n."/table-alias prefix on purpose -- the two
# existence-check queries select bare "nspname" straight off
# pg_namespace, while every joined catalog-fingerprint section that also
# touches pg_namespace always qualifies it as "n.nspname" (via a JOIN
# alias). That distinction is what keeps this stub from accidentally
# intercepting -- and blanking out -- the real fingerprint sections below.
if [[ "$SQL" == *"FROM pg_namespace"* && "$SQL" == *"WHERE nspname IN"* ]]; then
  # Multi-schema existence pre-check, before the first apply: always
  # empty -- the fake schemas are never actually created by this stub.
  exit 0
fi

if [[ "$SQL" == *"FROM pg_namespace"* && "$SQL" == *"WHERE nspname ="* ]]; then
  if [[ -f "$STATE_DIR/schemas_exist" ]]; then
    echo "1"
  fi
  exit 0
fi

if [[ "$SQL" == *"schema_migrations"* && "$SQL" == *"duration_ms"* && "$SQL" == *"ORDER BY version"* ]]; then
  [[ -f "$STATE_DIR/history.txt" ]] && cat "$STATE_DIR/history.txt"
  exit 0
fi

if [[ "$SQL" == *"contype = 'f'"* && "$SQL" == *"COUNT(*) OVER"* ]]; then
  [[ -f "$STATE_DIR/fk_catalog.txt" ]] && cat "$STATE_DIR/fk_catalog.txt"
  exit 0
fi

# Every other query the harness issues is one of the 13 comprehensive
# catalog-fingerprint sections (or the migration-history table's own
# structure, captured as part of that same fingerprint). None of them
# need distinct, realistic content for a control-flow test -- only a
# value that changes if and only if the scenario says the catalog
# drifted between the two runs.
[[ -f "$STATE_DIR/fingerprint.txt" ]] && cat "$STATE_DIR/fingerprint.txt"
exit 0
PSQL_EOF
chmod +x "$FAKE_REPO/bin/psql"

# --- Build the baseline FK catalog rows from the harness's own
#     EXPECTED_FKS list: table|conname|f|t|t|1 (present exactly once,
#     type 'f', deferrable, initially deferred).
run_case() {
  local name="$1" scenario="$2" expect="$3" grep_for="${4:-}"

  local state_dir
  state_dir="$(mktemp -d)"
  awk -F: '{ print $1 "|" $2 "|f|t|t|1" }' "$FK_PAIRS_FILE" > "$state_dir/fk_catalog_baseline.txt"

  local dsn_stub="postgresql://stub:stub@127.0.0.1:5/stub"
  local output rc
  set +e
  output="$(
    PATH="$FAKE_REPO/bin:$PATH" \
    PG_DSN="$dsn_stub" \
    HARNESS_STUB_STATE_DIR="$state_dir" \
    HARNESS_STUB_SCENARIO="$scenario" \
    bash "$FAKE_REPO/scripts/tests/test_schema_constraint_idempotency.sh" 2>&1
  )"
  rc=$?
  set -e
  rm -rf "$state_dir"

  case "$expect" in
    pass)
      if [[ "$rc" -ne 0 ]]; then
        fail_case "scenario '$name' (${scenario}): expected the harness to PASS but it exited ${rc}. Output:\n${output}"
        return
      fi
      if ! grep -q "^PASS:" <<<"$output"; then
        fail_case "scenario '$name' (${scenario}): harness exited 0 but did not print a PASS: line. Output:\n${output}"
        return
      fi
      ;;
    fail)
      if [[ "$rc" -eq 0 ]]; then
        fail_case "scenario '$name' (${scenario}): expected the harness to FAIL but it exited 0. Output:\n${output}"
        return
      fi
      if [[ -n "$grep_for" ]] && ! grep -qF "$grep_for" <<<"$output"; then
        fail_case "scenario '$name' (${scenario}): harness failed as expected, but its output did not contain the expected reason (${grep_for@Q}). Output:\n${output}"
        return
      fi
      ;;
  esac
  echo "ok: $name"
}

echo "--- Running harness control-flow scenarios (no database involved) ---"

run_case "true idempotent rerun passes" \
  "good" pass

run_case "first run that applies nothing is rejected" \
  "first_run_applies_nothing" fail \
  "first migration run did not report applying any migrations"

run_case "second run reporting an applied migration is rejected" \
  "second_run_applies" fail \
  "second migration run did not report zero pending migrations"

run_case "second run without the exact no-op message is rejected" \
  "second_run_wrong_message" fail \
  "second migration run did not report zero pending migrations"

run_case "second run exiting nonzero is rejected" \
  "second_run_nonzero_exit" fail \
  "second migration run exited nonzero"

run_case "history changing across the second run is rejected" \
  "history_drifts" fail \
  "migration history changed between the first and second"

run_case "execution_count changing across the second run is rejected" \
  "execution_count_drifts" fail \
  "migration history changed between the first and second"

run_case "catalog fingerprint drifting across the second run is rejected" \
  "fingerprint_drifts" fail \
  "complete catalog fingerprint changed between the first and second"

run_case "a foreign key disappearing across the second run is rejected" \
  "fk_drifts" fail \
  "focused foreign-key assertion(s) failed"

run_case "a pending migration in the final status is rejected" \
  "final_status_pending" fail \
  "final status reports pending one-time migration(s)"

echo ""
if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAIL: ${FAILURES} harness control-flow scenario(s) behaved incorrectly (see above)." >&2
  exit 1
fi

echo "PASS: the real scripts/tests/test_schema_constraint_idempotency.sh correctly accepts a true"
echo "      idempotent rerun and correctly rejects every scenario tested: a first run that applies"
echo "      nothing, a second run that applies a migration, a second run with the wrong no-op"
echo "      message, a nonzero second-run exit, a drifted migration-history snapshot (including a"
echo "      changed execution_count), a drifted catalog fingerprint, a missing foreign key, and a"
echo "      pending migration surviving into the final status check."
