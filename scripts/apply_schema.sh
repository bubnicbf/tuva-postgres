#!/usr/bin/env bash
set -euo pipefail

: "${PG_DSN:?PG_DSN not set (export in .env)}"
PG_SCHEMA="${PG_SCHEMA:-tuva}"

# 1) Ensure schema exists
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS ${PG_SCHEMA};"

# 2) Apply per-table SQL files in deterministic order
shopt -s nullglob
mapfile -t files < <(ls -1 db/tables/*.sql | sort)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No table files found at db/tables/*.sql"
  exit 1
fi

for f in "${files[@]}"; do
  echo "Applying: $f"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -v schema="$PG_SCHEMA" -f "$f"
done

echo "Schema applied to ${PG_SCHEMA}."
