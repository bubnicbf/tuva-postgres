#!/usr/bin/env bash
# Structural regression test for migration execution modes (one_time vs
# repeatable -- see src/tuva_postgres/migrations.py's module docstring).
#
# Drives the real, committed discover()/compute_checksum()/ExecutionMode
# implementation against the real repository -- not a synthetic fixture --
# so a manifest missing its 'execution' field, an unexpected mode on an
# existing migration, or a checksum drift on an already-applied migration
# fails this test directly. Complements (does not duplicate)
# scripts/tests/test_versioned_migration_layout.sh, which owns the
# broader file-layout/orphan/duplicate-reference assertions; this test's
# sole focus is the execution-mode contract.
#
# Sibling database-backed coverage: tests/integration/test_pipeline_
# integration.py's TestMigrationExecutionModesIntegration class proves
# actual one_time/repeatable/ordering/mode-immutability/history-upgrade
# *behavior* against a real, disposable PostgreSQL database using fixture
# migrations (never the real ones) -- this script proves the *real
# manifests themselves* declare the contract correctly, without a
# database.
#
# Database-free: no PostgreSQL, PG_DSN, network, or external services
# required. Only the Python standard library is used (discover()/
# compute_checksum() never import psycopg -- that only happens lazily
# inside functions that actually talk to Postgres).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Pinned checksums for migrations 0001 and 0002 (same values pinned by
# test_versioned_migration_layout.sh) -- re-asserted here specifically to
# prove that adding the required 'execution' field to both real manifests
# did not change either checksum (compute_checksum() hashes only a
# migration's constituent SQL files, never its manifest JSON).
EXPECTED_0001_CHECKSUM="6a7cfe125ac4becc4e18000ced22530394ed0d5bda0b7898928820bce83a0445"
EXPECTED_0002_CHECKSUM="fd8a57293ec70b8f78a9347ec2eba571d5b375100cd665c733dc2d536397a2e4"

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
from tuva_postgres.migrations import discover, compute_checksum, ExecutionMode  # noqa: E402
from tuva_postgres.errors import MigrationError  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


migrations_dir = (repo_root / "db" / "migrations").resolve()

# --- 1) every real manifest discovers successfully, meaning every one of
#        them satisfies discover()'s required, validated 'execution' field
#        (missing/unknown/wrong-type values raise MigrationError; see
#        tests/unit/test_migrations.py's TestManifestExecutionValidation
#        for the database-free unit coverage of that validation itself). -
try:
    migrations = discover(migrations_dir, repo_root)
except MigrationError as exc:
    print(f"FAIL: real repository migrations failed to discover: {exc}")
    sys.exit(1)

check(len(migrations) >= 2, f"expected at least 2 migrations, found {len(migrations)}")

# --- 2) every manifest's raw JSON explicitly declares 'execution' as one
#        of the allowed string values -- re-checked directly against the
#        raw JSON (not just the parsed MigrationDef) so a future
#        discover() regression that silently defaulted a missing field
#        would still be caught here. -----------------------------------
allowed_values = {m.value for m in ExecutionMode}
for manifest_path in sorted(migrations_dir.glob("*.json")):
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    check(
        "execution" in raw,
        f"{manifest_path.name}: manifest JSON has no 'execution' key",
    )
    check(
        raw.get("execution") in allowed_values,
        f"{manifest_path.name}: 'execution'={raw.get('execution')!r} is not one of {sorted(allowed_values)}",
    )

# --- 3) migrations 0001 and 0002 are specifically 'one_time' -- the
#        compatibility rule this whole feature must never violate: every
#        migration that existed before execution-mode tracking was added
#        must be marked one_time. -----------------------------------------
by_version = {m.version: m for m in migrations}
for version in ("0001", "0002"):
    check(version in by_version, f"migration {version} not found")
    if version in by_version:
        check(
            by_version[version].execution is ExecutionMode.ONE_TIME,
            f"migration {version} must be execution=one_time, found {by_version[version].execution!r}",
        )

# --- 4) 0001/0002 checksums are unaffected by adding the 'execution'
#        field -- a database that already recorded these migrations must
#        never see a checksum mismatch because of this feature. ----------
if "0001" in by_version:
    actual_0001 = compute_checksum(by_version["0001"])
    check(
        actual_0001 == expected_0001,
        f"migration 0001 checksum changed after adding 'execution'! expected {expected_0001}, got {actual_0001}",
    )
if "0002" in by_version:
    actual_0002 = compute_checksum(by_version["0002"])
    check(
        actual_0002 == expected_0002,
        f"migration 0002 checksum changed after adding 'execution'! expected {expected_0002}, got {actual_0002}",
    )

# --- 5) no no-op/placeholder migration was added solely to exercise the
#        repeatable code path in production -- repeatable-mode testing
#        must use fixtures/dependency injection (see
#        TestMigrationExecutionModesIntegration), never a real committed
#        migration. As of this feature, the only real migrations are
#        0001 and 0002, and both are one_time; assert that directly so a
#        future placeholder migration 0003 (or any real 'repeatable'
#        migration added only for test coverage) fails this test loudly.
real_versions = sorted(by_version)
check(
    real_versions == ["0001", "0002"],
    f"expected exactly migrations ['0001', '0002'] in the real repository, found {real_versions} -- "
    "if this is a legitimate new migration, update this assertion; if it exists only to exercise "
    "repeatable-mode code paths, remove it and use fixtures/dependency injection instead "
    "(see TestMigrationExecutionModesIntegration in tests/integration/test_pipeline_integration.py)",
)
repeatable_versions = [v for v, m in by_version.items() if m.execution is ExecutionMode.REPEATABLE]
check(
    repeatable_versions == [],
    f"found real, committed repeatable migration(s) {repeatable_versions} -- if none of these are "
    "legitimate production migrations, they must not be added solely to test the repeatable code path",
)

if failures:
    print("FAIL: migration execution-mode violations found:\n")
    for f in failures:
        print(f"  - {f}")
    print(f"\n{len(failures)} failure(s).")
    sys.exit(1)

print(
    f"PASS: {len(migrations)} real repository migration(s) discovered (versions {real_versions}), "
    "every manifest declares a valid 'execution' field, migrations 0001/0002 are both execution=one_time "
    "with checksums unchanged by adding that field, and no real committed migration uses execution=repeatable "
    "(repeatable-mode coverage lives entirely in fixture-based tests, never in production migrations):"
)
print(f"  0001 = {expected_0001} (one_time)")
print(f"  0002 = {expected_0002} (one_time)")
PY
STATUS=$?
set -e

cat "$OUT_TMP"
if [[ $STATUS -ne 0 ]]; then
  exit 1
fi
