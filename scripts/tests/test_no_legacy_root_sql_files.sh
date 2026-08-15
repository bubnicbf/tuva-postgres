#!/usr/bin/env bash
# Regression test guarding against the obsolete top-level SQL files being
# reintroduced.
#
# db/schema.sql and db/tests.sql were monolithic, example-only leftovers
# from before the per-table schema/tests layout existed. They were fully
# superseded by:
#   - db/migrations/sql/0001_baseline/**/*.sql (the version-owned baseline
#     migration DDL, see db/migrations/0001_baseline.json and
#     scripts/apply_schema.sh) -- db/migrations/ is the sole authoritative
#     home for deployable DDL
#   - db/tests/*.sql (validation/query SQL, executed by scripts/run_tests.sh)
# db/seed.sql was removed earlier still (see commit 0853cd8) once the
# loader stopped running a post-load seed step.
#
# Nothing in this repo should ever need any of these three files again --
# git history is the archive. This test fails loudly if one reappears at
# the repo root, so a future contributor doesn't accidentally recreate a
# stale, unmaintained duplicate of the modular schema/tests.
#
# No PostgreSQL, PG_DSN, network access, or external services required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

declare -a LEGACY_FILES=(
  "db/schema.sql"
  "db/seed.sql"
  "db/tests.sql"
)

for f in "${LEGACY_FILES[@]}"; do
  if [[ -e "$REPO_ROOT/$f" ]]; then
    fail "obsolete legacy file '$f' has reappeared at the repo root -- it was removed in favor of db/migrations/sql/**/*.sql (deployable DDL) and db/tests/*.sql (validation SQL) (git history is the archive; do not recreate it)"
  fi
done

echo "PASS: db/schema.sql, db/seed.sql, and db/tests.sql all remain absent."
