#!/usr/bin/env bash
set -euo pipefail

: "${PG_DSN:?PG_DSN not set (export in .env)}"
PG_SCHEMA="${PG_SCHEMA:-tuva}"
TERMINOLOGY_SCHEMA="${TERMINOLOGY_SCHEMA:-${PG_SCHEMA}_term}"

# 1) Ensure schemas exist
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS ${PG_SCHEMA};"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS ${TERMINOLOGY_SCHEMA};"

# 2) Apply core per-table SQL files (db/tables)
apply_folder() {
  local folder="$1"
  local extra_vars=("${@:2}")
  shopt -s nullglob
  mapfile -t files < <(ls -1 "$folder"/*.sql 2>/dev/null | sort)
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "No SQL files found in $folder (skipping)"
    return 0
  fi
  for f in "${files[@]}"; do
    echo "Applying: $f"
    psql "$PG_DSN" -v ON_ERROR_STOP=1 "${extra_vars[@]}" -f "$f"
  done
}

# Core schema files
apply_folder "db/tables"        -v schema="$PG_SCHEMA"

# Terminology schema files
apply_folder "db/terminology"   -v terminology_schema="$TERMINOLOGY_SCHEMA"

echo "Schema applied to ${PG_SCHEMA}; terminology applied to ${TERMINOLOGY_SCHEMA}."
