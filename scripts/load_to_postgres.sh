#!/usr/bin/env bash
# Loads the managed CSV snapshot into Postgres.
#
# This loader treats the CSVs in DATA_DIR as a complete, replaceable
# snapshot -- not an append-only stream. A complete snapshot is loaded in
# one PostgreSQL transaction: the managed tables are truncated together
# and every CSV is copied in, then committed only if every copy succeeds.
# Re-running the same snapshot is therefore safe (no duplicate-key
# errors), and a failure partway through leaves the previous data intact
# (Postgres rolls back the whole transaction).
set -euo pipefail

: "${PG_DSN:?PG_DSN not set}"
: "${PG_SCHEMA:?PG_SCHEMA not set}"
: "${DATA_DIR:?DATA_DIR not set}"

# --- Managed table list (single source of truth) ----------------------------
# Dependency-aware order, kept for clarity/defense-in-depth: practitioner
# and location have no foreign keys; patient has none either; encounter
# references patient and practitioner, so it's listed after them; every
# other table references only patient/encounter/practitioner (never each
# other). In practice all of these foreign keys are DEFERRABLE INITIALLY
# DEFERRED, so within a single transaction Postgres only validates them at
# COMMIT -- but keeping a sensible order still makes the script easier to
# reason about and matches the table's own DDL apply order.
declare -a MANAGED_TABLES=(
  "practitioner"
  "location"
  "patient"
  "encounter"
  "person_id_crosswalk"
  "medical_claim"
  "pharmacy_claim"
  "eligibility"
  "procedure"
  "observation"
  "lab_result"
  "condition"
  "medication"
  "immunization"
  "appointment"
)

# --- Identifier safety: validate PG_SCHEMA before it ever reaches SQL ------
if ! [[ "$PG_SCHEMA" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "ERROR: PG_SCHEMA='${PG_SCHEMA}' is not a safe PostgreSQL identifier." >&2
  echo "       Expected: starts with a letter or underscore, then letters/digits/underscores only." >&2
  exit 1
fi

# Single-quote a string for use as a \copy / SQL string literal, doubling
# any embedded single quotes. \copy's filename argument follows normal SQL
# string-literal quoting, so this is the same escaping strategy as for any
# other SQL string constant -- verified against paths containing spaces
# (and embedded quotes) in scripts/tests/test_load_to_postgres_atomic.sh.
sql_quote_literal() {
  local s="$1" escaped
  escaped=$(printf '%s' "$s" | sed "s/'/''/g")
  printf "'%s'" "$escaped"
}

# --- Input preflight: decide before connecting to Postgres or touching data -
declare -a present_tables=()
declare -a missing_csvs=()
declare -a unreadable_csvs=()

for t in "${MANAGED_TABLES[@]}"; do
  csv="${DATA_DIR}/${t}.csv"
  if [[ -f "$csv" ]]; then
    if [[ -r "$csv" ]]; then
      present_tables+=("$t")
    else
      unreadable_csvs+=("$csv")
    fi
  else
    missing_csvs+=("$csv")
  fi
done

if [[ ${#unreadable_csvs[@]} -gt 0 ]]; then
  echo "ERROR: found CSV file(s) that are not readable. Refusing to load." >&2
  for f in "${unreadable_csvs[@]}"; do
    echo "  - ${f}" >&2
  done
  exit 1
fi

if [[ ${#present_tables[@]} -eq 0 ]]; then
  echo "No seed CSVs found in ${DATA_DIR} (looked for ${#MANAGED_TABLES[@]} managed table(s)); nothing to load."
  exit 0
fi

if [[ ${#missing_csvs[@]} -gt 0 ]]; then
  echo "ERROR: partial snapshot in ${DATA_DIR}: ${#present_tables[@]} of ${#MANAGED_TABLES[@]} managed CSV(s) present." >&2
  echo "       Partial snapshot loads are rejected to avoid loading an incomplete dataset." >&2
  echo "       Missing CSV(s):" >&2
  for f in "${missing_csvs[@]}"; do
    echo "  - ${f}" >&2
  done
  exit 1
fi

echo "Preflight OK: all ${#MANAGED_TABLES[@]} managed CSV(s) present and readable in ${DATA_DIR}."

# --- Ensure schema exists (safe, explicit, outside the data transaction) ---
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS \"${PG_SCHEMA}\";"

# --- Build the atomic snapshot-replacement transaction ----------------------
TMP_SQL="$(mktemp "${TMPDIR:-/tmp}/load_to_postgres.XXXXXX.sql")"
cleanup() {
  rm -f "$TMP_SQL"
}
trap cleanup EXIT

WITH_OPTS="WITH (FORMAT csv, HEADER true, NULL '', QUOTE '\"', ESCAPE '\"')"

{
  echo "BEGIN;"
  echo

  # One combined TRUNCATE for every managed table: Postgres requires all
  # tables linked by foreign keys to be truncated together (or CASCADE,
  # which we deliberately avoid so an unexpected external dependency
  # causes a safe failure/rollback instead of silently cascading deletes).
  echo "TRUNCATE TABLE"
  last_index=$((${#MANAGED_TABLES[@]} - 1))
  for i in "${!MANAGED_TABLES[@]}"; do
    t="${MANAGED_TABLES[$i]}"
    if [[ "$i" -eq "$last_index" ]]; then
      printf '  "%s"."%s";\n' "$PG_SCHEMA" "$t"
    else
      printf '  "%s"."%s",\n' "$PG_SCHEMA" "$t"
    fi
  done
  echo

  for t in "${MANAGED_TABLES[@]}"; do
    csv="${DATA_DIR}/${t}.csv"
    quoted_path="$(sql_quote_literal "$csv")"
    printf '\\copy "%s"."%s" FROM %s %s\n' "$PG_SCHEMA" "$t" "$quoted_path" "$WITH_OPTS"
  done
  echo

  echo "COMMIT;"
} > "$TMP_SQL"

echo "Starting atomic snapshot replacement for schema \"${PG_SCHEMA}\" (${#MANAGED_TABLES[@]} table(s)):"
for t in "${MANAGED_TABLES[@]}"; do
  echo "  - ${t} <- ${DATA_DIR}/${t}.csv"
done

# One database session, one transaction. If any \copy (or the truncate,
# or a deferred constraint at COMMIT) fails, ON_ERROR_STOP causes psql to
# stop and exit nonzero without ever reaching COMMIT; Postgres
# automatically rolls back the still-open transaction when the session
# ends, so the previous snapshot is left untouched. No per-table
# transactions, no commits between tables, no ON CONFLICT DO NOTHING.
psql "$PG_DSN" -v ON_ERROR_STOP=1 -f "$TMP_SQL"

echo "Snapshot transaction committed."
echo "Load complete."
