"""`tuva-postgres healthcheck`: a single, fast, no-secrets status check
suitable for a container/Kubernetes health probe or a human running it by
hand.

Checks, in order (each can independently fail the check):
  1. PostgreSQL connectivity (can we open a connection at all).
  2. Migration state: nothing pending, and no checksum mismatches.
  3. Freshness: the most recent *successful* pipeline run finished within
     PIPELINE_MAX_SUCCESS_AGE_HOURS.

Never prints PG_DSN, API tokens, or any other secret -- only the derived
pass/fail booleans and small operational facts (timestamps, counts).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import migrations, ops
from .db import connect


@dataclass
class HealthCheckResult:
    db_connect_ok: bool
    migrations_ok: bool
    migrations_detail: str
    freshness_ok: bool
    freshness_detail: str

    @property
    def healthy(self) -> bool:
        return self.db_connect_ok and self.migrations_ok and self.freshness_ok

    def render(self) -> str:
        lines = [
            f"db_connect: {'OK' if self.db_connect_ok else 'FAIL'}",
            f"migrations: {'OK' if self.migrations_ok else 'FAIL'} ({self.migrations_detail})",
            f"freshness:  {'OK' if self.freshness_ok else 'FAIL'} ({self.freshness_detail})",
            f"overall:    {'HEALTHY' if self.healthy else 'UNHEALTHY'}",
        ]
        return "\n".join(lines)


def run_healthcheck(config, *, connect_fn=connect, migrations_mod=migrations, ops_mod=ops) -> HealthCheckResult:
    try:
        conn = connect_fn(config.pg_dsn)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any connect failure is "unhealthy"
        return HealthCheckResult(
            db_connect_ok=False,
            migrations_ok=False,
            migrations_detail="skipped (no database connection)",
            freshness_ok=False,
            freshness_detail="skipped (no database connection)",
        )

    try:
        migrations_ok = True
        try:
            mstatus = migrations_mod.status(conn, config)
            if mstatus.checksum_mismatches:
                migrations_ok = False
                migrations_detail = f"checksum mismatch(es): {', '.join(mstatus.checksum_mismatches)}"
            elif mstatus.pending:
                migrations_ok = False
                migrations_detail = f"{len(mstatus.pending)} pending migration(s)"
            else:
                migrations_detail = f"{len(mstatus.applied)} applied, none pending"
        except Exception as exc:  # noqa: BLE001
            migrations_ok = False
            migrations_detail = f"status check failed ({exc.__class__.__name__})"

        freshness_ok = True
        try:
            last_success = ops_mod.latest_successful_run(conn, config.ops_schema)
            if last_success is None:
                freshness_ok = False
                freshness_detail = "no successful run recorded"
            else:
                _run_id, finished_at, *_rest = last_success
                if finished_at is None:
                    freshness_ok = False
                    freshness_detail = "last successful run has no finished_at timestamp"
                else:
                    age_hours = (datetime.now(timezone.utc) - finished_at).total_seconds() / 3600.0
                    if age_hours > config.pipeline_max_success_age_hours:
                        freshness_ok = False
                        freshness_detail = (
                            f"last success {age_hours:.1f}h ago exceeds "
                            f"PIPELINE_MAX_SUCCESS_AGE_HOURS={config.pipeline_max_success_age_hours}"
                        )
                    else:
                        freshness_detail = f"last success {age_hours:.1f}h ago"
        except Exception as exc:  # noqa: BLE001
            freshness_ok = False
            freshness_detail = f"freshness check failed ({exc.__class__.__name__})"

        return HealthCheckResult(
            db_connect_ok=True,
            migrations_ok=migrations_ok,
            migrations_detail=migrations_detail,
            freshness_ok=freshness_ok,
            freshness_detail=freshness_detail,
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    from .config import REQUIRE_DB, PipelineConfig

    config = PipelineConfig.load(required=REQUIRE_DB)
    result = run_healthcheck(config)
    print(result.render())
    return 0 if result.healthy else 1


if __name__ == "__main__":  # pragma: no cover
    import sys as _sys

    _sys.exit(main())
