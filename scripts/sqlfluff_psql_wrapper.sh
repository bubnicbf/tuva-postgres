#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-lint}"
shift || true

if ! command -v sqlfluff >/dev/null 2>&1; then
  echo "sqlfluff not found on PATH" >&2
  exit 1
fi

fail=0
for f in "$@"; do
  # Only touch .sql files
  case "$f" in
    *.sql) ;;
    *) continue ;;
  esac

  # Read & normalize on the fly
  # - Replace psql vars :"schema" → public, :"terminology_schema" → terminology
  # - Replace timestamp_ntz → timestamp (for linting only)
  norm="$(python3 - "$f" << 'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")

# psql vars
s = re.sub(r':"schema"', 'public', s)
s = re.sub(r':"terminology_schema"', 'terminology', s)

# common psql var in search_path lines (harmless if duplicated)
s = re.sub(r'SET\s+search_path\s+TO\s+public,\s*public;', 'SET search_path TO public;', s, flags=re.IGNORECASE)

# non-Postgres type used in some core files
s = re.sub(r'\btimestamp_ntz\b', 'timestamp', s, flags=re.IGNORECASE)

# A tiny nicety: Snowflake-ish "number" → numeric for linting (optional)
s = re.sub(r'\bnumber\b', 'numeric', s, flags=re.IGNORECASE)

sys.stdout.write(s)
PY
)"

  # Pipe normalized text to sqlfluff, preserving original path for rule ignores, etc.
  if [ "$MODE" = "lint" ]; then
    printf "%s" "$norm" | sqlfluff lint - --stdin-filename "$f" || fail=1
  else
    # future-proofing: allow "format" to preview fixes without touching files
    printf "%s" "$norm" | sqlfluff fix - --stdin-filename "$f" --force || fail=1
  fi
done

exit $fail
