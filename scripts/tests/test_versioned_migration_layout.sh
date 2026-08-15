#!/usr/bin/env bash
# Structural regression test for the versioned-migration-DDL architecture.
#
# db/migrations/ is the sole authoritative home for deployable DDL: every
# DDL file is owned by exactly one migration version, manifest ordering is
# explicit and deterministic, deployable SQL lives only under
# db/migrations/sql/ (never a separate mutable directory like the retired
# db/tables/), and applied migrations 0001/0002 are checksum-pinned so a
# database that already recorded them never sees a mismatch.
#
# This drives the real, committed discovery/checksum implementation
# (tuva_postgres.migrations.discover / compute_checksum) against the real
# repository -- not a synthetic fixture -- so a broken manifest, an
# orphaned SQL file, a stray db/tables/ reference, or a checksum drift on
# an already-applied migration fails this test directly.
#
# Database-free: no PostgreSQL, PG_DSN, network, or external services
# required. Only the Python standard library is used (migrations.py's
# psycopg dependency is imported lazily inside functions that actually
# talk to Postgres -- discover()/compute_checksum() never touch it).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Pinned pre-refactor checksums for migrations 0001 and 0002 (captured with
# this same compute_checksum implementation against db/tables/*.sql /
# db/migrations/sql/0002_operational_schema.sql before they were moved into
# version-owned directories). These must never change -- a database that
# already recorded these migrations must never see a checksum mismatch.
EXPECTED_0001_CHECKSUM="6a7cfe125ac4becc4e18000ced22530394ed0d5bda0b7898928820bce83a0445"
EXPECTED_0002_CHECKSUM="fd8a57293ec70b8f78a9347ec2eba571d5b375100cd665c733dc2d536397a2e4"

# Plain `python3` (standard library only, like the other structural tests
# in this suite -- e.g. test_constraint_idempotency_guards.py): discover()/
# compute_checksum() never import psycopg (that only happens lazily inside
# functions that actually talk to Postgres), so no `uv sync` or locked
# environment is required here.
OUT_TMP="$(mktemp)"
cleanup() { rm -f "$OUT_TMP"; }
trap cleanup EXIT

set +e
python3 - "$REPO_ROOT" "$EXPECTED_0001_CHECKSUM" "$EXPECTED_0002_CHECKSUM" > "$OUT_TMP" 2>&1 <<'PY'
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
expected_0001 = sys.argv[2]
expected_0002 = sys.argv[3]

sys.path.insert(0, str(repo_root / "src"))
from tuva_postgres.migrations import discover, compute_checksum  # noqa: E402
from tuva_postgres.errors import MigrationError  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


migrations_dir = (repo_root / "db" / "migrations").resolve()
sql_root = (migrations_dir / "sql").resolve()

# --- 1) real repository migrations discover successfully -------------------
try:
    migrations = discover(migrations_dir, repo_root)
except MigrationError as exc:
    print(f"FAIL: real repository migrations failed to discover: {exc}")
    sys.exit(1)

check(len(migrations) >= 2, f"expected at least 2 migrations, found {len(migrations)}")

# --- 2) versions are unique and sorted deterministically --------------------
versions = [m.version for m in migrations]
check(len(versions) == len(set(versions)), f"duplicate versions found: {versions}")
check(versions == sorted(versions), f"migrations are not sorted by version: {versions}")

# --- 3) every manifest filename/version combination follows convention -----
# "{version}_{slug}.json" -- the manifest's own filename must start with
# its declared version, and its directory under db/migrations/sql/ must be
# named after the manifest's filename stem (enforced by discover() itself,
# re-asserted here against the real files for a direct, readable failure).
for m in migrations:
    stem = m.manifest_path.stem
    check(
        stem.startswith(m.version + "_"),
        f"{m.manifest_path.name}: filename does not start with its own version "
        f"{m.version!r} (expected '{m.version}_...')",
    )
    owned_dir = sql_root / stem
    check(
        owned_dir.is_dir(),
        f"{m.manifest_path.name}: expected version-owned directory not found: {owned_dir}",
    )

# --- 4) every manifest references at least one file -------------------------
for m in migrations:
    check(len(m.files) >= 1, f"{m.manifest_path.name}: references zero files")

# --- 5) & 6) every referenced file exists, and is under db/migrations/sql/ --
# (discover() itself enforces both of these -- reaching this point with a
# populated `migrations` list already proves it -- but assert explicitly so
# a future weakening of discover()'s enforcement is caught here too.)
all_referenced: list[Path] = []
for m in migrations:
    for f in m.files:
        check(f.is_file(), f"{m.manifest_path.name}: referenced file does not exist: {f}")
        check(
            f.is_relative_to(sql_root),
            f"{m.manifest_path.name}: referenced file is not under db/migrations/sql/: {f}",
        )
        # --- 7) each referenced file belongs to the directory for its OWNING
        #        migration version specifically (not just any migration's).
        owned_dir = sql_root / m.manifest_path.stem
        check(
            f.is_relative_to(owned_dir),
            f"{m.manifest_path.name}: referenced file {f} is outside its own "
            f"version-owned directory {owned_dir}",
        )
        all_referenced.append(f)

# --- 8) no migration manifest references db/tables/ -------------------------
for m in migrations:
    raw_text = m.manifest_path.read_text(encoding="utf-8")
    check("db/tables" not in raw_text, f"{m.manifest_path.name}: still references db/tables/")

# --- 9) no deployable DDL remains under db/tables/ ---------------------------
legacy_tables_dir = repo_root / "db" / "tables"
check(not legacy_tables_dir.exists(), f"legacy directory still exists: {legacy_tables_dir}")

# --- 10) every committed migration SQL file is referenced by exactly one
#         manifest: detect both orphaned files and duplicate references. ----
if sql_root.is_dir():
    all_sql_files = sorted(sql_root.rglob("*.sql"))
else:
    all_sql_files = []

referenced_counts: dict[Path, int] = {}
for f in all_referenced:
    referenced_counts[f] = referenced_counts.get(f, 0) + 1

orphaned = [f for f in all_sql_files if referenced_counts.get(f, 0) == 0]
duplicated = [f for f, count in referenced_counts.items() if count > 1]

check(not orphaned, f"orphaned migration SQL file(s) not referenced by any manifest: {orphaned}")
check(not duplicated, f"migration SQL file(s) referenced by more than one manifest: {duplicated}")

# --- 11) & 12) migration 0001 and 0002 checksums are exactly the
#               pre-refactor (pre-move) checksums -----------------------------
by_version = {m.version: m for m in migrations}
if "0001" in by_version:
    actual_0001 = compute_checksum(by_version["0001"])
    check(
        actual_0001 == expected_0001,
        f"migration 0001 checksum changed! expected {expected_0001}, got {actual_0001} "
        "-- this would produce a checksum mismatch for any database that already applied 0001",
    )
else:
    failures.append("migration 0001 not found")

if "0002" in by_version:
    actual_0002 = compute_checksum(by_version["0002"])
    check(
        actual_0002 == expected_0002,
        f"migration 0002 checksum changed! expected {expected_0002}, got {actual_0002} "
        "-- this would produce a checksum mismatch for any database that already applied 0002",
    )
else:
    failures.append("migration 0002 not found")

# --- 13) validation SQL under db/tests/ is not discovered as migration DDL --
db_tests_dir = (repo_root / "db" / "tests").resolve()
check(db_tests_dir.is_dir(), f"expected validation SQL directory not found: {db_tests_dir}")
for f in all_referenced:
    check(
        not f.is_relative_to(db_tests_dir),
        f"a migration manifest references a file under db/tests/ (validation SQL, not DDL): {f}",
    )

# --- 14) the active SQL test runner uses db/tests/, not db/tables/tests/ ----
run_tests_sh = (repo_root / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
check("db/tests/" in run_tests_sh, "scripts/run_tests.sh does not reference db/tests/")
check(
    "db/tables/tests" not in run_tests_sh,
    "scripts/run_tests.sh still references the retired db/tables/tests/ path",
)

if failures:
    print("FAIL: versioned migration layout violations found:\n")
    for f in failures:
        print(f"  - {f}")
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)

print(
    f"PASS: {len(migrations)} real repository migration(s) discovered "
    f"(versions {versions}), every referenced file lives under its own "
    "version-owned db/migrations/sql/ directory with no orphans or "
    "duplicate references, no manifest references db/tables/, db/tables/ "
    "no longer exists, db/tests/ validation SQL is never discovered as "
    "migration DDL, scripts/run_tests.sh uses db/tests/, and migrations "
    "0001/0002 checksums exactly match their pre-refactor values:"
)
print(f"  0001 = {expected_0001}")
print(f"  0002 = {expected_0002}")
PY
STATUS=$?
set -e

cat "$OUT_TMP"
if [[ $STATUS -ne 0 ]]; then
  exit 1
fi
