#!/usr/bin/env bash
# Regression test for db/migrations/sql/0001_baseline/core/patient.sql and
# db/tests/patient_gender_addon.sql
#
# Guards against the patient table's index drifting out of sync with its
# column model. patient.sql defines "gender varchar" (there is no "sex"
# column), but an index previously read:
#   CREATE INDEX IF NOT EXISTS patient_sex_idx ON :"schema".patient (sex);
# which references a nonexistent column and would fail when the DDL is
# applied. A related test, patient_gender_addon.sql, had the same drift:
# it referenced p.sex even though the current model uses p.gender.
#
# This is a dependency-free, source-level check -- no PostgreSQL, network,
# or external tooling required. It reads the two SQL files as text and
# confirms the DDL/test are internally consistent with the "gender" model.
#
# Usage:
#   scripts/tests/test_patient_gender_index.sh [repo_root]
#
# With no argument, validates the real repository (located relative to
# this script). An optional repo_root argument lets a negative-control
# check point this test at a scratch fixture instead, without ever
# modifying the real, committed SQL files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_ROOT="${1:-$REPO_ROOT}"

PATIENT_SQL="$TARGET_ROOT/db/migrations/sql/0001_baseline/core/patient.sql"
ADDON_SQL="$TARGET_ROOT/db/tests/patient_gender_addon.sql"

fail() {
  echo "FAIL: $1" >&2
  if [[ -n "${2:-}" ]]; then
    echo "----- context -----" >&2
    echo "$2" >&2
    echo "-------------------" >&2
  fi
  exit 1
}

if [[ ! -f "$PATIENT_SQL" ]]; then
  fail "expected file not found: $PATIENT_SQL"
fi
if [[ ! -f "$ADDON_SQL" ]]; then
  fail "expected file not found: $ADDON_SQL"
fi

# --- 1) patient.sql declares a "gender" column -----------------------------
if ! grep -qE '^[[:space:]]*gender[[:space:]]+varchar' "$PATIENT_SQL"; then
  fail "patient.sql does not declare a 'gender varchar' column" \
    "$(grep -nE 'gender|sex' "$PATIENT_SQL" || true)"
fi

# --- 2)-4) patient.sql has a correctly-formed patient_gender_idx -----------
GENDER_IDX_LINE="$(grep -E '^CREATE INDEX IF NOT EXISTS[[:space:]]+patient_gender_idx' "$PATIENT_SQL" || true)"
if [[ -z "$GENDER_IDX_LINE" ]]; then
  fail "no 'CREATE INDEX IF NOT EXISTS patient_gender_idx' declaration found in patient.sql" \
    "$(grep -nE 'CREATE INDEX' "$PATIENT_SQL" || true)"
fi

if ! grep -qE 'ON[[:space:]]+:"schema"\.patient' <<<"$GENDER_IDX_LINE"; then
  fail "patient_gender_idx does not target :\"schema\".patient" "$GENDER_IDX_LINE"
fi

if ! grep -qE '\(gender\)' <<<"$GENDER_IDX_LINE"; then
  fail "patient_gender_idx does not index the (gender) column" "$GENDER_IDX_LINE"
fi

# --- 5) reject a lingering obsolete patient_sex_idx -------------------------
if grep -q 'patient_sex_idx' "$PATIENT_SQL"; then
  fail "patient.sql still contains an obsolete 'patient_sex_idx' declaration" \
    "$(grep -n 'patient_sex_idx' "$PATIENT_SQL")"
fi

# --- 6) reject any current index expression targeting patient (sex) --------
if grep -qE 'patient[[:space:]]*\(sex\)' "$PATIENT_SQL"; then
  fail "patient.sql still contains an index expression targeting patient (sex)" \
    "$(grep -nE 'patient[[:space:]]*\(sex\)' "$PATIENT_SQL")"
fi

# --- 7) reject p.sex in the patient gender add-on test ----------------------
if grep -qE '\bp\.sex\b' "$ADDON_SQL"; then
  fail "patient_gender_addon.sql still references p.sex" \
    "$(grep -nE '\bp\.sex\b' "$ADDON_SQL")"
fi

# --- 8) confirm the add-on test uses p.gender --------------------------------
if ! grep -qE '\bp\.gender\b' "$ADDON_SQL"; then
  fail "patient_gender_addon.sql does not reference p.gender" \
    "$(cat "$ADDON_SQL")"
fi

echo "PASS: patient.sql declares gender, indexes it via patient_gender_idx on"
echo "      :\"schema\".patient (gender), carries no obsolete patient_sex_idx or"
echo "      patient (sex) index, and patient_gender_addon.sql consistently uses"
echo "      p.gender instead of p.sex."
