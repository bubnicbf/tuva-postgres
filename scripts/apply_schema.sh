#!/usr/bin/env bash
# Compatibility wrapper: delegates to the migration runner
# (src/tuva_postgres/migrations.py), which applies the version-owned DDL
# under db/migrations/sql/ referenced by db/migrations/*.json manifests.
# This is the single authoritative schema-deployment path -- `make
# create-db`, `tuva-postgres migrate`, and this script all end up here.
# See db/migrations/ and docs/RUNBOOK.md.
#
# Usage:
#   bash scripts/apply_schema.sh                    # apply pending migrations
#   bash scripts/apply_schema.sh --status            # read-only status
#   bash scripts/apply_schema.sh --baseline-existing  # explicit baseline of a pre-existing, compatible DB
set -euo pipefail

: "${PG_DSN:?PG_DSN not set (export in .env)}"
export PG_SCHEMA="${PG_SCHEMA:-tuva}"
export TERMINOLOGY_SCHEMA="${TERMINOLOGY_SCHEMA:-${PG_SCHEMA}_term}"
export OPS_SCHEMA="${OPS_SCHEMA:-tuva_ops}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer `uv run` (locked psycopg) when available; fall back to plain
# python3 with src/ on PYTHONPATH otherwise (e.g. inside a container that
# already has the package installed).
if command -v uv >/dev/null 2>&1 && [[ -f "$REPO_ROOT/uv.lock" ]]; then
  exec uv run python3 -m tuva_postgres.migrations "$@"
else
  export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"
  exec python3 -m tuva_postgres.migrations "$@"
fi
