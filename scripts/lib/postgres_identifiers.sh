#!/usr/bin/env bash
# Shared shell-side PostgreSQL identifier validation -- the Bash
# equivalent of src/tuva_postgres/identifiers.py's single authoritative
# policy. Every script under scripts/ that accepts a dynamic schema name
# from configuration (PG_SCHEMA, TERMINOLOGY_SCHEMA, OPS_SCHEMA, ...) or
# generates one itself (the scripts/tests/*.sh integration tests that
# create their own uniquely-named disposable schemas) sources this file
# instead of keeping its own copy of the identifier regex.
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/lib/postgres_identifiers.sh"   # (adjust the
#     # relative path to lib/ from wherever the sourcing script lives)
#   validate_postgres_identifier "$PG_SCHEMA" "PG_SCHEMA"
#   qualified="$(quote_validated_postgres_identifier "$PG_SCHEMA").$(quote_validated_postgres_identifier "$table")"
#
# This file is a library, meant to be sourced -- it defines functions and
# a constant, and does nothing (in particular: never connects to a
# database, never uses eval) if executed directly.
set -u

# Same ASCII policy as identifiers.py's IDENTIFIER_PATTERN: first
# character a letter or underscore, remaining characters letters/digits/
# underscores only. No dots (a schema and a relation are always two
# independently validated identifiers, never one "schema.table" value),
# no quotes, no whitespace, no semicolons, no SQL comment markers, no
# null bytes, no non-ASCII characters -- anything outside this character
# class is rejected by construction, not by an explicit denylist.
readonly POSTGRES_IDENTIFIER_PATTERN='^[A-Za-z_][A-Za-z0-9_]*$'

# validate_postgres_identifier <value> <label>
#
# Prints an actionable error naming <label> and exits the calling script
# with status 1 if <value> does not match the shared identifier policy.
# This is a fail-fast preflight check meant to run before the calling
# script ever invokes psql -- never after. Never mutates <value>; the
# identifier that reaches SQL is exactly the identifier this validated,
# unchanged.
validate_postgres_identifier() {
  local value="${1:-}" label="${2:?validate_postgres_identifier requires a label argument}"
  if [[ -z "$value" ]]; then
    echo "ERROR: ${label} is empty -- refusing to use it as a SQL identifier." >&2
    exit 1
  fi
  if ! [[ "$value" =~ $POSTGRES_IDENTIFIER_PATTERN ]]; then
    echo "ERROR: ${label}='${value}' is not a safe SQL identifier." >&2
    echo "       Expected: ASCII letters/digits/underscores only, starting with a letter or" >&2
    echo "       underscore -- no dots, quotes, whitespace, semicolons, comments, or other" >&2
    echo "       special characters." >&2
    exit 1
  fi
}

# quote_validated_postgres_identifier <value>
#
# Prints <value> wrapped in double quotes for use as a single SQL
# identifier, doubling any embedded double quote as PostgreSQL requires.
# Callers MUST call validate_postgres_identifier on <value> first -- this
# function does not validate on its own, it only quotes. In practice a
# value that already passed validation can never contain a `"` (the
# pattern above disallows it entirely), so the doubling below is a
# defense-in-depth no-op, not something callers should rely on for
# safety by itself.
quote_validated_postgres_identifier() {
  local value="$1" escaped
  escaped=$(printf '%s' "$value" | sed 's/"/""/g')
  printf '"%s"' "$escaped"
}
