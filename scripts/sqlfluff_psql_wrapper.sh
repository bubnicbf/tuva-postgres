#!/usr/bin/env bash
# SQLFluff cannot parse psql-style `:"name"`/`:'name'` variable
# placeholders (see migrations/*.sql and src/tuva_ingest/db.py's
# substitute_psql_vars) -- they aren't valid PostgreSQL syntax on their
# own. This wrapper normalizes each file's placeholders to a
# syntactically valid stand-in *only for linting*, pipes the result to
# `sqlfluff` on stdin (so the file on disk is never modified), and
# reports lint failures against the original filename.
#
# Keep the substitution list in sync with every `:"..."`/`:'...'`
# variable actually used under migrations/ (see
# src/tuva_ingest/migrations.py's `_resolve_vars`): raw_schema,
# ops_schema, ingest_role, transform_role.
set -euo pipefail

MODE="${1:-lint}"
shift || true

if ! command -v sqlfluff >/dev/null 2>&1; then
  echo "sqlfluff not found on PATH" >&2
  exit 1
fi

fail=0
for f in "$@"; do
  case "$f" in
    *.sql) ;;
    *) continue ;;
  esac

  norm="$(python3 - "$f" << 'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")

# psql identifier-form vars (:"name") -> a syntactically valid stand-in
s = re.sub(r':"raw_schema"', 'raw', s)
s = re.sub(r':"ops_schema"', 'ingest_ops', s)
s = re.sub(r':"ingest_role"', 'tuva_ingest_role', s)
s = re.sub(r':"transform_role"', 'tuva_transform_role', s)

# psql literal-form vars (:'name') -> a quoted string literal stand-in
s = re.sub(r":'ingest_role'", "'tuva_ingest_role'", s)
s = re.sub(r":'transform_role'", "'tuva_transform_role'", s)

sys.stdout.write(s)
PY
)"

  if [ "$MODE" = "lint" ]; then
    printf "%s" "$norm" | sqlfluff lint - --dialect postgres --stdin-filename "$f" || fail=1
  else
    printf "%s" "$norm" | sqlfluff fix - --dialect postgres --stdin-filename "$f" --force || fail=1
  fi
done

exit $fail
