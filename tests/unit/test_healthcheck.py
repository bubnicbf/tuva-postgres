"""Unit tests for tuva_postgres.healthcheck using injected fake
connect/migrations/ops dependencies -- no real PostgreSQL required."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres import healthcheck  # noqa: E402
from tuva_postgres.migrations import MigrationStatus  # noqa: E402


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _config(max_age_hours=30.0):
    return SimpleNamespace(pg_dsn="postgresql://x/y", ops_schema="tuva_ops", pipeline_max_success_age_hours=max_age_hours)


class TestHealthCheck(unittest.TestCase):
    def test_db_connection_failure_is_unhealthy(self):
        def connect_fn(dsn):
            raise RuntimeError("connection refused")

        result = healthcheck.run_healthcheck(_config(), connect_fn=connect_fn)
        self.assertFalse(result.db_connect_ok)
        self.assertFalse(result.healthy)

    def test_healthy_when_no_pending_migrations_and_recent_success(self):
        conn = _FakeConn()
        fake_migrations = SimpleNamespace(
            status=lambda c, cfg: MigrationStatus(applied=(), pending=(), checksum_mismatches=())
        )
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        fake_ops = SimpleNamespace(latest_successful_run=lambda c, schema: ("r1", recent, 15, 100, "{}", 5, 0))

        result = healthcheck.run_healthcheck(
            _config(), connect_fn=lambda dsn: conn, migrations_mod=fake_migrations, ops_mod=fake_ops,
        )
        self.assertTrue(result.healthy)
        self.assertTrue(conn.closed)

    def test_unhealthy_on_checksum_mismatch(self):
        conn = _FakeConn()
        fake_migrations = SimpleNamespace(
            status=lambda c, cfg: MigrationStatus(applied=(), pending=(), checksum_mismatches=("0001",))
        )
        fake_ops = SimpleNamespace(latest_successful_run=lambda c, schema: None)

        result = healthcheck.run_healthcheck(
            _config(), connect_fn=lambda dsn: conn, migrations_mod=fake_migrations, ops_mod=fake_ops,
        )
        self.assertFalse(result.migrations_ok)
        self.assertFalse(result.healthy)

    def test_unhealthy_on_pending_migrations(self):
        conn = _FakeConn()
        pending_stub = SimpleNamespace(version="0002")
        fake_migrations = SimpleNamespace(
            status=lambda c, cfg: MigrationStatus(applied=(), pending=(pending_stub,), checksum_mismatches=())
        )
        fake_ops = SimpleNamespace(latest_successful_run=lambda c, schema: None)

        result = healthcheck.run_healthcheck(
            _config(), connect_fn=lambda dsn: conn, migrations_mod=fake_migrations, ops_mod=fake_ops,
        )
        self.assertFalse(result.migrations_ok)

    def test_unhealthy_when_no_successful_run_recorded(self):
        conn = _FakeConn()
        fake_migrations = SimpleNamespace(
            status=lambda c, cfg: MigrationStatus(applied=(), pending=(), checksum_mismatches=())
        )
        fake_ops = SimpleNamespace(latest_successful_run=lambda c, schema: None)

        result = healthcheck.run_healthcheck(
            _config(), connect_fn=lambda dsn: conn, migrations_mod=fake_migrations, ops_mod=fake_ops,
        )
        self.assertFalse(result.freshness_ok)
        self.assertFalse(result.healthy)

    def test_unhealthy_when_last_success_too_old(self):
        conn = _FakeConn()
        fake_migrations = SimpleNamespace(
            status=lambda c, cfg: MigrationStatus(applied=(), pending=(), checksum_mismatches=())
        )
        stale = datetime.now(timezone.utc) - timedelta(hours=100)
        fake_ops = SimpleNamespace(latest_successful_run=lambda c, schema: ("r1", stale, 15, 100, "{}", 5, 0))

        result = healthcheck.run_healthcheck(
            _config(max_age_hours=30.0), connect_fn=lambda dsn: conn, migrations_mod=fake_migrations, ops_mod=fake_ops,
        )
        self.assertFalse(result.freshness_ok)

    def test_render_never_includes_dsn(self):
        conn = _FakeConn()
        fake_migrations = SimpleNamespace(
            status=lambda c, cfg: MigrationStatus(applied=(), pending=(), checksum_mismatches=())
        )
        fake_ops = SimpleNamespace(latest_successful_run=lambda c, schema: None)
        result = healthcheck.run_healthcheck(
            _config(), connect_fn=lambda dsn: conn, migrations_mod=fake_migrations, ops_mod=fake_ops,
        )
        self.assertNotIn("postgresql://", result.render())


if __name__ == "__main__":
    unittest.main()
