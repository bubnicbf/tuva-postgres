"""Standard-library unit tests for tuva_ingest.healthcheck.run_healthcheck,
using injected fake connect_fn/migrations_mod/state_mod (see the
function's own signature) rather than a real database connection.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.healthcheck import run_healthcheck  # noqa: E402


@dataclass
class _FakeConfig:
    pg_dsn: str = "postgresql://x@localhost/db"
    ops_schema: str = "ingest_ops"
    pipeline_max_success_age_hours: float = 30.0


class _FakeConn:
    def close(self):
        pass


@dataclass
class _FakeMigrationStatus:
    applied: tuple = ()
    pending: tuple = ()
    checksum_mismatches: tuple = ()


class _FakeMigrationsModule:
    def __init__(self, status_result):
        self._status_result = status_result

    def status(self, conn, config):
        if isinstance(self._status_result, Exception):
            raise self._status_result
        return self._status_result


class _FakeStateModule:
    def __init__(self, latest_successful_run_result):
        self._result = latest_successful_run_result

    def latest_successful_run(self, conn, ops_schema):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _connect_ok(dsn):
    return _FakeConn()


def _connect_fails(dsn):
    raise RuntimeError("connection refused")


class TestRunHealthcheck(unittest.TestCase):
    def test_db_connect_failure_short_circuits_everything(self):
        result = run_healthcheck(
            _FakeConfig(),
            connect_fn=_connect_fails,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus()),
            state_mod=_FakeStateModule(None),
        )
        self.assertFalse(result.db_connect_ok)
        self.assertFalse(result.healthy)
        self.assertIn("skipped", result.migrations_detail)

    def test_fully_healthy(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        run_row = ("run-1", "snap-1", recent, {"eligibility": 3}, ["eligibility"])
        result = run_healthcheck(
            _FakeConfig(),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus(applied=("001", "002", "003"))),
            state_mod=_FakeStateModule(run_row),
        )
        self.assertTrue(result.healthy)
        self.assertTrue(result.db_connect_ok)
        self.assertTrue(result.migrations_ok)
        self.assertTrue(result.freshness_ok)

    def test_pending_migrations_marks_unhealthy(self):
        class _Pending:
            version = "004"

        result = run_healthcheck(
            _FakeConfig(),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus(pending=(_Pending(),))),
            state_mod=_FakeStateModule(None),
        )
        self.assertFalse(result.migrations_ok)
        self.assertIn("pending", result.migrations_detail)
        self.assertFalse(result.healthy)

    def test_checksum_mismatch_marks_unhealthy(self):
        result = run_healthcheck(
            _FakeConfig(),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus(checksum_mismatches=("001",))),
            state_mod=_FakeStateModule(None),
        )
        self.assertFalse(result.migrations_ok)
        self.assertIn("checksum mismatch", result.migrations_detail)

    def test_no_successful_run_marks_unhealthy(self):
        result = run_healthcheck(
            _FakeConfig(),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus()),
            state_mod=_FakeStateModule(None),
        )
        self.assertFalse(result.freshness_ok)
        self.assertIn("no successful run", result.freshness_detail)

    def test_stale_successful_run_marks_unhealthy(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=100)
        run_row = ("run-1", "snap-1", stale, {}, [])
        result = run_healthcheck(
            _FakeConfig(pipeline_max_success_age_hours=30.0),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus()),
            state_mod=_FakeStateModule(run_row),
        )
        self.assertFalse(result.freshness_ok)
        self.assertIn("exceeds", result.freshness_detail)

    def test_render_never_includes_dsn(self):
        result = run_healthcheck(
            _FakeConfig(pg_dsn="postgresql://user:secret-pass@localhost/db"),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(_FakeMigrationStatus()),
            state_mod=_FakeStateModule(None),
        )
        rendered = result.render()
        self.assertNotIn("secret-pass", rendered)

    def test_migrations_status_exception_marks_unhealthy_not_raised(self):
        result = run_healthcheck(
            _FakeConfig(),
            connect_fn=_connect_ok,
            migrations_mod=_FakeMigrationsModule(RuntimeError("boom")),
            state_mod=_FakeStateModule(None),
        )
        self.assertFalse(result.migrations_ok)
        self.assertIn("status check failed", result.migrations_detail)


if __name__ == "__main__":
    unittest.main()
