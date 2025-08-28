#!/usr/bin/env bash
set -euo pipefail

: "${PG_DSN:?PG_DSN not set (export in .env)}"
PG_SCHEMA="${PG_SCHEMA:-tuva}"
TERMINOLOGY_SCHEMA="${TERMINOLOGY_SCHEMA:-${PG_SCHEMA}_term}"

shopt -s nullglob
mapfile -t files < <(ls -1 db/tests/*.sql | sort)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No SQL tests found in db/tests/"
  exit 0
fi

for f in "${files[@]}"; do
  echo "==> Running ${f}"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -v schema="$PG_SCHEMA" -v terminology_schema="$TERMINOLOGY_SCHEMA" -f "$f"
done

echo "All tests executed."
