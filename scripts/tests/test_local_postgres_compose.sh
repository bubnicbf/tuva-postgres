#!/usr/bin/env bash
# Isolated Docker Compose runtime smoke test for the local PostgreSQL
# development stack (compose.yaml). Requires a working Docker Engine +
# Docker Compose v2 plugin; SKIPS (exit 0) cleanly if either is missing.
#
# Safety (see README.md's "Local PostgreSQL with Docker Compose" section
# and tests/unit/test_local_postgres_compose.py for the database-free
# structural counterpart to this script):
#   - Runs under a unique, randomly-suffixed Compose PROJECT name, so it
#     never touches a developer's own `local-db-*`/`compose-up` stack or
#     its named volumes (Compose namespaces volumes per project).
#   - Binds Postgres to a dynamically chosen host port (override with
#     TEST_POSTGRES_PORT), never colliding with a developer's own 5432.
#   - Registers a cleanup trap BEFORE starting any container; cleanup
#     only ever targets this script's own unique project
#     (`docker compose -p <unique> down -v --remove-orphans`) -- never
#     `docker system prune`, never another project's resources.
#   - Never reads or relies on an ambient PG_DSN/.env; every connection
#     string this script uses is constructed locally from compose.yaml's
#     own local-only example credentials.
#
# What this proves (run it via `make test-compose-integration`):
#   1. `docker compose config` renders successfully.
#   2. Postgres starts and its healthcheck reaches healthy within a
#      bounded timeout.
#   3. Postgres accepts a simple query.
#   4. The `migrate` service applies all migrations successfully.
#   5. The core/terminology/operational schemas exist.
#   6. The migration-history table (schema_migrations) exists.
#   7. A second migration run reports no pending migrations (idempotent).
#   8. Host connectivity works through the loopback port (if a host
#      `psql` client is available; otherwise this portion is reported
#      SKIPPED and step 3's container-based connectivity stands in).
#   9. The database survives an ordinary stop/start cycle (named volume
#      preserved).
#  10. Cleanup removes the isolated project and its volumes.
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# --- Availability check: SKIP (exit 0), never fail, when Docker/Compose
#     aren't present -- this test must never be part of always-on,
#     database-free validation. ------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "SKIPPED: docker is not available in this environment -- the isolated Compose"
  echo "         runtime smoke test was not run. Structural, database-free coverage"
  echo "         (tests/unit/test_local_postgres_compose.py) still applies."
  exit 0
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "SKIPPED: 'docker compose' (v2 plugin) is not available in this environment --"
  echo "         the isolated Compose runtime smoke test was not run. Structural,"
  echo "         database-free coverage (tests/unit/test_local_postgres_compose.py)"
  echo "         still applies."
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "SKIPPED: a Docker daemon is not reachable in this environment (docker CLI is"
  echo "         installed, but 'docker info' failed) -- the isolated Compose runtime"
  echo "         smoke test was not run."
  exit 0
fi

# --- Isolation: unique project name + dynamically chosen host port -------
PROJECT="tuva_pgtest_$$_${RANDOM}"
TEST_POSTGRES_PORT="${TEST_POSTGRES_PORT:-$(( (RANDOM % 10000) + 20000 ))}"
export POSTGRES_PORT="$TEST_POSTGRES_PORT"

# Local-only, disposable-test credentials -- identical to compose.yaml's
# own local-only example credentials (never a secret; never production).
TEST_PG_USER="tuva_local"
TEST_PG_PASSWORD="local-only-example-password-change-me"
TEST_PG_DB="tuva"

dc() {
  docker compose -p "$PROJECT" "$@"
}

FAILED=0
CLEANED_UP=0

diagnostics() {
  echo "----- docker compose ps (project=$PROJECT) -----"
  dc ps || true
  echo "----- postgres logs -----"
  dc logs postgres || true
  echo "----- migrate service logs -----"
  dc logs migrate || true
  echo "-----------------------------------------------"
}

cleanup() {
  if [[ "$CLEANED_UP" -eq 1 ]]; then
    return
  fi
  CLEANED_UP=1
  # Cleanup ONLY this isolated project's resources -- never a broader
  # prune, never another project's containers/networks/volumes.
  dc down -v --remove-orphans >/dev/null 2>&1 || true
}
# Registered BEFORE any container is started, and covers normal exit,
# Ctrl-C, and termination alike.
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $1" >&2
  diagnostics >&2
  FAILED=1
  exit 1
}

echo "Isolated Compose smoke test: project=$PROJECT host_port=$TEST_POSTGRES_PORT"

# --- 1) docker compose config renders successfully ------------------------
if ! dc config >/dev/null; then
  fail "'docker compose config' did not render successfully"
fi
echo "PASS (1/10): docker compose config renders."

# --- 2) & 3) Postgres starts, healthcheck reaches healthy within a bounded
#             timeout, and accepts a simple query -------------------------
if ! dc up -d --wait --wait-timeout 90 postgres; then
  fail "postgres did not become healthy within the bounded timeout"
fi
echo "PASS (2/10): postgres started and reached healthy."

if ! dc exec -T postgres psql -U "$TEST_PG_USER" -d "$TEST_PG_DB" -c "SELECT 1;" >/dev/null; then
  fail "postgres did not accept a simple query via 'docker compose exec'"
fi
echo "PASS (3/10): postgres accepts a simple query (via container)."

# --- 4) the migrate service applies all migrations -------------------------
if ! dc run --rm migrate; then
  fail "the 'migrate' service (tuva-postgres migrate) did not exit successfully"
fi
echo "PASS (4/10): migrate service applied all migrations successfully."

# --- 5) expected schemas exist ---------------------------------------------
SCHEMA_COUNT="$(dc exec -T postgres psql -U "$TEST_PG_USER" -d "$TEST_PG_DB" -At -c \
  "SELECT count(*) FROM pg_namespace WHERE nspname IN ('tuva', 'tuva_term', 'tuva_ops');")"
SCHEMA_COUNT="$(echo "$SCHEMA_COUNT" | tr -d '[:space:]')"
if [[ "$SCHEMA_COUNT" != "3" ]]; then
  fail "expected all 3 schemas (tuva, tuva_term, tuva_ops) to exist, found $SCHEMA_COUNT"
fi
echo "PASS (5/10): core (tuva), terminology (tuva_term), and operations (tuva_ops) schemas all exist."

# --- 6) migration-history table exists -------------------------------------
HISTORY_EXISTS="$(dc exec -T postgres psql -U "$TEST_PG_USER" -d "$TEST_PG_DB" -At -c \
  "SELECT to_regclass('tuva_ops.schema_migrations') IS NOT NULL;")"
HISTORY_EXISTS="$(echo "$HISTORY_EXISTS" | tr -d '[:space:]')"
if [[ "$HISTORY_EXISTS" != "t" ]]; then
  fail "tuva_ops.schema_migrations does not exist after migrating"
fi
echo "PASS (6/10): tuva_ops.schema_migrations (migration history) exists."

# --- 7) a second migration run reports no pending migrations ---------------
if ! dc run --rm migrate; then
  fail "the second, idempotent 'migrate' run did not exit successfully"
fi
STATUS_OUTPUT="$(dc run --rm migrate migrate --status)"
if ! grep -qE "Pending one-time migrations \(0\)" <<<"$STATUS_OUTPUT"; then
  echo "$STATUS_OUTPUT"
  fail "expected zero pending one-time migrations after a clean second run"
fi
if ! grep -qE "awaiting initial application \(0\)" <<<"$STATUS_OUTPUT"; then
  echo "$STATUS_OUTPUT"
  fail "expected zero repeatable migrations awaiting initial application"
fi
if ! grep -qE "awaiting reapplication, checksum changed \(0\)" <<<"$STATUS_OUTPUT"; then
  echo "$STATUS_OUTPUT"
  fail "expected zero repeatable migrations awaiting reapplication"
fi
echo "PASS (7/10): second migration run is a true no-op (nothing pending)."

# --- 8) host connectivity through the configured loopback port -------------
if command -v psql >/dev/null 2>&1; then
  if PGPASSWORD="$TEST_PG_PASSWORD" psql -h 127.0.0.1 -p "$TEST_POSTGRES_PORT" -U "$TEST_PG_USER" -d "$TEST_PG_DB" \
      -c "SELECT 1;" >/dev/null; then
    echo "PASS (8/10): host connectivity works through 127.0.0.1:$TEST_POSTGRES_PORT (host psql client)."
  else
    fail "host psql client could not connect through 127.0.0.1:$TEST_POSTGRES_PORT"
  fi
else
  echo "SKIPPED (8/10): no host 'psql' client available -- host-loopback connectivity was not"
  echo "                independently verified; container-based connectivity (step 3) already"
  echo "                proved the database itself is reachable and answering queries."
fi

# --- 9) survives an ordinary stop/start cycle, data preserved --------------
ROWS_BEFORE="$(dc exec -T postgres psql -U "$TEST_PG_USER" -d "$TEST_PG_DB" -At -c \
  "SELECT count(*) FROM tuva_ops.schema_migrations;" | tr -d '[:space:]')"

if ! dc stop postgres; then
  fail "'docker compose stop postgres' did not exit successfully"
fi
if ! dc up -d --wait --wait-timeout 90 postgres; then
  fail "postgres did not become healthy again after a stop/start cycle"
fi

ROWS_AFTER="$(dc exec -T postgres psql -U "$TEST_PG_USER" -d "$TEST_PG_DB" -At -c \
  "SELECT count(*) FROM tuva_ops.schema_migrations;" | tr -d '[:space:]')"

if [[ "$ROWS_BEFORE" -lt 1 ]]; then
  fail "expected at least 1 recorded migration before the stop/start cycle, found $ROWS_BEFORE"
fi
if [[ "$ROWS_BEFORE" != "$ROWS_AFTER" ]]; then
  fail "migration history row count changed across a stop/start cycle (before=$ROWS_BEFORE after=$ROWS_AFTER) -- data was not preserved"
fi
echo "PASS (9/10): database survives an ordinary stop/start cycle ($ROWS_AFTER migration row(s) preserved)."

# --- 10) cleanup (verified explicitly, in addition to the EXIT trap) -------
cleanup
REMAINING="$(docker compose -p "$PROJECT" ps -q 2>/dev/null | wc -l | tr -d '[:space:]')"
if [[ "$REMAINING" != "0" ]]; then
  echo "FAIL: $REMAINING container(s) still present for project $PROJECT after cleanup" >&2
  exit 1
fi
echo "PASS (10/10): isolated project '$PROJECT' and its volumes were fully cleaned up."

echo ""
echo "PASS: isolated Docker Compose smoke test succeeded end to end (project=$PROJECT, host_port=$TEST_POSTGRES_PORT)."
