#!/usr/bin/env bash
# Regression test for scripts/apply_schema.sh
#
# apply_schema.sh used to shell out to `psql` directly, applying
# db/tables/*.sql files itself. It is now a compatibility wrapper that
# delegates to the migration runner (`python3 -m tuva_postgres.migrations`,
# see db/migrations/ and src/tuva_postgres/migrations.py) -- the single
# authoritative schema-deployment path. This test verifies that
# delegation behaviorally, by stubbing `uv` and `python3` and inspecting
# what apply_schema.sh actually invokes, rather than requiring a real
# PostgreSQL server or a real `uv sync`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

STUB_BIN_DIR="$TMP_DIR/bin"
export INVOCATION_LOG="$TMP_DIR/invocations.log"
mkdir -p "$STUB_BIN_DIR"
: > "$INVOCATION_LOG"

cat > "$STUB_BIN_DIR/uv" <<'STUB'
#!/usr/bin/env bash
printf 'uv %s\n' "$*" >> "$INVOCATION_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/uv"

cat > "$STUB_BIN_DIR/python3" <<'STUB'
#!/usr/bin/env bash
printf 'python3 %s | PYTHONPATH=%s\n' "$*" "${PYTHONPATH:-}" >> "$INVOCATION_LOG"
exit 0
STUB
chmod +x "$STUB_BIN_DIR/python3"

run_wrapper() {
  # Runs apply_schema.sh with the stub bin dir first on PATH (so both
  # stubs are found before any real uv/python3), plus whatever caller env
  # is prefixed. Always inside `if`/`while` per this repo's errexit-safety
  # convention -- never as a bare statement.
  : > "$INVOCATION_LOG"
  PATH="$STUB_BIN_DIR:$PATH" \
    PG_DSN="postgresql://test:test@localhost:5432/testdb" \
    bash "$REPO_ROOT/scripts/apply_schema.sh" "$@" > "$TMP_DIR/stdout" 2>&1
}

fail() {
  echo "FAIL: $1" >&2
  echo "----- apply_schema.sh output -----" >&2
  cat "$TMP_DIR/stdout" >&2
  echo "----- recorded invocations -----" >&2
  cat "$INVOCATION_LOG" >&2
  echo "-----------------------------------" >&2
  exit 1
}

# --- 1) PG_DSN is required -------------------------------------------------
set +e
env -u PG_DSN PATH="$STUB_BIN_DIR:$PATH" bash "$REPO_ROOT/scripts/apply_schema.sh" > "$TMP_DIR/no_dsn.stdout" 2>&1
NO_DSN_STATUS=$?
set -e
if [[ $NO_DSN_STATUS -eq 0 ]]; then
  fail "apply_schema.sh succeeded without PG_DSN set (expected a fail-fast error)"
fi
if ! grep -qF "PG_DSN not set" "$TMP_DIR/no_dsn.stdout"; then
  fail "missing PG_DSN did not produce a clear 'PG_DSN not set' error"
fi

# --- 2) delegates via `uv run` when uv is on PATH (this repo's real path) -
if ! run_wrapper; then
  fail "apply_schema.sh exited nonzero with uv stubbed as present"
fi
if ! grep -qF "uv run python3 -m tuva_postgres.migrations" "$INVOCATION_LOG"; then
  fail "apply_schema.sh did not delegate to 'uv run python3 -m tuva_postgres.migrations' when uv is available"
fi

# --- 3) falls back to plain python3 (with src/ on PYTHONPATH) when uv is --
#        NOT on PATH
NO_UV_BIN_DIR="$TMP_DIR/bin_no_uv"
mkdir -p "$NO_UV_BIN_DIR"
cp "$STUB_BIN_DIR/python3" "$NO_UV_BIN_DIR/python3"
: > "$INVOCATION_LOG"
if ! PATH="$NO_UV_BIN_DIR:/usr/bin:/bin" \
  PG_DSN="postgresql://test:test@localhost:5432/testdb" \
  bash "$REPO_ROOT/scripts/apply_schema.sh" > "$TMP_DIR/no_uv.stdout" 2>&1; then
  fail "apply_schema.sh exited nonzero when uv is unavailable (expected a python3 fallback)"
fi
if ! grep -qF -- "-m tuva_postgres.migrations" "$INVOCATION_LOG"; then
  fail "apply_schema.sh did not fall back to 'python3 -m tuva_postgres.migrations' when uv is unavailable"
fi
if ! grep -qF "src" "$INVOCATION_LOG"; then
  fail "python3 fallback invocation did not include src/ on PYTHONPATH"
fi

# --- 4) --status and --baseline-existing pass through as arguments --------
if ! run_wrapper --status; then
  fail "apply_schema.sh --status exited nonzero"
fi
if ! grep -qF -- "--status" "$INVOCATION_LOG"; then
  fail "apply_schema.sh did not pass --status through to the migration runner"
fi

if ! run_wrapper --baseline-existing; then
  fail "apply_schema.sh --baseline-existing exited nonzero"
fi
if ! grep -qF -- "--baseline-existing" "$INVOCATION_LOG"; then
  fail "apply_schema.sh did not pass --baseline-existing through to the migration runner"
fi

# --- 5) schema env var defaults ---------------------------------------------
# apply_schema.sh execs into python3/uv, so its exported defaults can't be
# observed from a child process's environment after the fact; check the
# default-assignment lines directly instead (still a behavioral risk to
# catch: these lines are exactly what a reviewer would break by mistake).
if ! grep -qF 'PG_SCHEMA="${PG_SCHEMA:-tuva}"' "$REPO_ROOT/scripts/apply_schema.sh"; then
  fail "apply_schema.sh no longer defaults PG_SCHEMA to 'tuva'"
fi
if ! grep -qF 'TERMINOLOGY_SCHEMA="${TERMINOLOGY_SCHEMA:-${PG_SCHEMA}_term}"' "$REPO_ROOT/scripts/apply_schema.sh"; then
  fail "apply_schema.sh no longer defaults TERMINOLOGY_SCHEMA to \${PG_SCHEMA}_term"
fi
if ! grep -qF 'OPS_SCHEMA="${OPS_SCHEMA:-tuva_ops}"' "$REPO_ROOT/scripts/apply_schema.sh"; then
  fail "apply_schema.sh no longer defaults OPS_SCHEMA to 'tuva_ops'"
fi

echo "PASS: apply_schema.sh is a compatibility wrapper that delegates to"
echo "      'python3 -m tuva_postgres.migrations' (via 'uv run' when available,"
echo "      with a PYTHONPATH fallback otherwise), requires PG_DSN, passes"
echo "      through --status/--baseline-existing, and defaults PG_SCHEMA/"
echo "      TERMINOLOGY_SCHEMA/OPS_SCHEMA consistently with the rest of the repo."
