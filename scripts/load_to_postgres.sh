#!/usr/bin/env bash
set -euo pipefail

: "${PG_DSN:?PG_DSN not set}"
: "${PG_SCHEMA:?PG_SCHEMA not set}"
: "${DATA_DIR:?DATA_DIR not set}"

# Create schema if missing
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS ${PG_SCHEMA};"

# Ensure schema exists for this session
export PGOPTIONS="--search_path=${PG_SCHEMA},public"

# Example: load standard Tuva tables (adjust names to actual Tuva seed set)
declare -a tables=(
  "practitioner"
  "patient"
  "encounter"
  "person_id_crosswalk"
  "medical_claim"
  "pharmacy_claim"
  "eligibility"
  "procedure"
  "observation"
)

# Use \copy so the CSV can be read by the client (no server-side paths needed)
for t in "${tables[@]}"; do
  csv="${DATA_DIR}/${t}.csv"
  if [[ -f "$csv" ]]; then
    echo "Loading ${t} from ${csv}..."
    psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "\
      \copy ${PG_SCHEMA}.${t} FROM '${csv}' WITH (FORMAT csv, HEADER true, NULL '', QUOTE '\"', ESCAPE '\"')"
  else
    echo "WARN: Missing ${csv} (skipping)"
  fi
done

echo "Running post-load seed.sql (keys, indexes, constraints)…"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -f db/seed.sql

echo "Load complete."
