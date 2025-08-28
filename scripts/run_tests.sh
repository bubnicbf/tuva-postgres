#!/usr/bin/env bash
set -euo pipefail

: "${PG_DSN:?PG_DSN not set (export in .env)}"
PG_SCHEMA="${PG_SCHEMA:-tuva}"
TERMINOLOGY_SCHEMA="${TERMINOLOGY_SCHEMA:-${PG_SCHEMA}_term}"

# Generate a run id that's easy to grep in CI logs
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
TMP_DIR="tmp/test_results"
mkdir -p "$TMP_DIR"

# Ensure results table & views exist
psql "$PG_DSN" -v ON_ERROR_STOP=1 -v schema="$PG_SCHEMA" -f db/tests/zz_results.sql

# Execute each test file to CSV
shopt -s nullglob
mapfile -t files < <(ls -1 db/tests/*.sql | sort)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No SQL tests found in db/tests/"
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
python3 scripts/ingest_test_csv.py "$RUN_ID" "$TMP_DIR"/*.csv > "$NORMALIZED"

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
