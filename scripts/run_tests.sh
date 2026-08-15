#!/usr/bin/env bash
# Runs the SQL data-quality/validation suite under db/tests/ -- the single
# canonical location for SQL validation and test-harness SQL (never
# deployable DDL; that lives under versioned migrations in
# db/migrations/sql/ instead -- see README.md / docs/RUNBOOK.md).
set -euo pipefail

: "${PG_DSN:?PG_DSN not set (export in .env)}"
PG_SCHEMA="${PG_SCHEMA:-tuva}"
TERMINOLOGY_SCHEMA="${TERMINOLOGY_SCHEMA:-${PG_SCHEMA}_term}"

# Resolve every path from this script's own location, not the caller's
# current working directory, so `bash scripts/run_tests.sh` (or
# `bash /abs/path/to/run_tests.sh`) behaves identically regardless of the
# directory it's invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# The canonical SQL-test directory, defined once and reused for both
# discovery and execution below -- see db/tests/ in README.md /
# docs/RUNBOOK.md.
SQL_TEST_DIR="$REPO_ROOT/db/tests"

# Test-harness setup, not a validation case: (re)creates the test_results
# table and its summary views. Applied exactly once, before any
# validation case runs, and excluded from the discovery glob below so it
# never produces its own test-result CSV or row in test_results.
RESULTS_SETUP_SQL="$SQL_TEST_DIR/zz_results.sql"

# Generate a run id that's easy to grep in CI logs
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
TMP_DIR="$REPO_ROOT/tmp/test_results"
mkdir -p "$TMP_DIR"

# Ensure results table & views exist (setup -- applied once, not a test case)
psql "$PG_DSN" -v ON_ERROR_STOP=1 -v schema="$PG_SCHEMA" -f "$RESULTS_SETUP_SQL"

# Execute each validation SQL file to CSV, in deterministic lexical order,
# excluding the setup file above. (db/tests/00_helpers_npi.sql is also
# setup in spirit -- it only defines a helper function other test files
# call -- but unlike zz_results.sql it produces no test/pass rows of its
# own, so leaving it in this loop is harmless: its "00_" prefix sorts it
# first, which is exactly the ordering the functions it defines require,
# and scripts/ingest_test_csv.py already skips any CSV with no test/pass
# header when building the normalized result set.)
shopt -s nullglob
mapfile -t files < <(
  for f in "$SQL_TEST_DIR"/*.sql; do
    [[ "$f" == "$RESULTS_SETUP_SQL" ]] && continue
    printf '%s\n' "$f"
  done | sort
)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No SQL tests found in $SQL_TEST_DIR"
  exit 0
fi

for f in "${files[@]}"; do
  base="$(basename "$f")"
  out_csv="$TMP_DIR/${base%.sql}.csv"
  echo "==> Running ${f}"
  # --csv prints a header per SELECT; footer off avoids row count chatter
  psql "$PG_DSN" \
    -v ON_ERROR_STOP=1 \
    -v schema="$PG_SCHEMA" \
    -v terminology_schema="$TERMINOLOGY_SCHEMA" \
    --csv --no-align \
    --pset footer=off \
    -f "$f" > "$out_csv"
done

# Normalize and load into test_results
NORMALIZED="$TMP_DIR/normalized.csv"
python3 "$REPO_ROOT/scripts/ingest_test_csv.py" "$RUN_ID" "$TMP_DIR"/*.csv > "$NORMALIZED"

# COPY into Postgres
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "\
  \copy ${PG_SCHEMA}.test_results (run_id, suite, test, pass, payload)
  FROM '${NORMALIZED}' WITH (FORMAT csv, HEADER true)
"

# Print a short CI-friendly summary
echo "RUN_ID=${RUN_ID}"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -At -c "\
  SELECT 'summary|' || run_id || '|' ||
         SUM(CASE WHEN pass THEN 1 ELSE 0 END) || '|' ||
         SUM(CASE WHEN NOT pass THEN 1 ELSE 0 END) || '|' ||
         COUNT(*)
  FROM ${PG_SCHEMA}.test_results
  WHERE run_id = '${RUN_ID}'
  GROUP BY run_id;
"

echo "Per-suite breakdown:"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "SELECT * FROM ${PG_SCHEMA}.v_test_summary WHERE run_id = '${RUN_ID}' ORDER BY suite;"
