#!/usr/bin/env bash
# Regression test guarding against the obsolete top-level SQL files being
# reintroduced.
#
# db/schema.sql and db/tests.sql were monolithic, example-only leftovers
# from before the per-table schema/tests layout existed. They were fully
# superseded by:
#   - db/tables/*.sql and db/tables/terminology/*.sql (applied via the
#     baseline migration, see db/migrations/0001_baseline.json and
#     scripts/apply_schema.sh)
#   - db/tables/tests/*.sql (executed by scripts/run_tests.sh)
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
    fail "obsolete legacy file '$f' has reappeared at the repo root -- it was removed in favor of db/tables/*.sql, db/tables/terminology/*.sql, and db/tables/tests/*.sql (git history is the archive; do not recreate it)"
  fi
done

echo "PASS: db/schema.sql, db/seed.sql, and db/tests.sql all remain absent."
