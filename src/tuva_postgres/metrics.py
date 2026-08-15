"""Operational metrics: database-backed (via ops.py) plus an optional
Prometheus textfile exporter at METRICS_FILE.

No proprietary monitoring vendor is required or assumed -- the textfile
format is the standard `node_exporter`/`prometheus-pushgateway`
"textfile collector" convention: any Prometheus install can scrape it by
pointing a textfile collector at METRICS_FILE's directory. Written
atomically (temp file + rename) so a scraper never observes a
half-written file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ops


@dataclass
class PipelineMetrics:
    last_success_timestamp: float | None
    last_run_status: str | None
    last_run_duration_seconds: float | None
    last_artifact_count: int | None
    last_bytes_downloaded: int | None
    last_rows_loaded_total: int | None
    last_test_failures: int | None
    consecutive_failures: int


def compute_metrics(conn, ops_schema: str) -> PipelineMetrics:
    last = ops.latest_run(conn, ops_schema)
    last_success = ops.latest_successful_run(conn, ops_schema)
    consecutive = ops.consecutive_failures(conn, ops_schema)

    last_run_status = last[1] if last else None

    last_success_timestamp = None
    last_run_duration_seconds = None
    last_artifact_count = None
    last_bytes_downloaded = None
    last_rows_loaded_total = None
    last_test_failures = None

    if last_success:
        _run_id, finished_at, artifact_count, bytes_downloaded, rows_loaded, _tests_passed, tests_failed = last_success
        if finished_at is not None:
            last_success_timestamp = finished_at.timestamp()
        last_artifact_count = artifact_count
        last_bytes_downloaded = bytes_downloaded
        last_test_failures = tests_failed
        if rows_loaded:
            parsed = rows_loaded if isinstance(rows_loaded, dict) else json.loads(rows_loaded)
            last_rows_loaded_total = sum(int(v) for v in parsed.values())

    if last and last[2] is not None and last[3] is not None:
        last_run_duration_seconds = (last[3] - last[2]).total_seconds()

    return PipelineMetrics(
        last_success_timestamp=last_success_timestamp,
        last_run_status=last_run_status,
        last_run_duration_seconds=last_run_duration_seconds,
        last_artifact_count=last_artifact_count,
        last_bytes_downloaded=last_bytes_downloaded,
        last_rows_loaded_total=last_rows_loaded_total,
        last_test_failures=last_test_failures,
        consecutive_failures=consecutive,
    )


_STATUS_VALUES = ("running", "succeeded", "failed", "skipped")


def render_prometheus_textfile(metrics: PipelineMetrics) -> str:
    lines = []

    def gauge(name: str, help_text: str, value):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {0 if value is None else value}")

    gauge(
        "tuva_postgres_last_success_timestamp_seconds",
        "Unix timestamp of the last successful pipeline run (0 if never succeeded).",
        metrics.last_success_timestamp,
    )
    for status in _STATUS_VALUES:
        lines.append("# HELP tuva_postgres_last_run_status 1 for the last run's status, 0 otherwise.")
        lines.append("# TYPE tuva_postgres_last_run_status gauge")
        lines.append(
            f'tuva_postgres_last_run_status{{status="{status}"}} '
            f"{1 if metrics.last_run_status == status else 0}"
        )
    gauge(
        "tuva_postgres_last_run_duration_seconds",
        "Wall-clock duration of the last completed pipeline run.",
        metrics.last_run_duration_seconds,
    )
    gauge("tuva_postgres_last_artifact_count", "Number of artifacts in the last successful run.", metrics.last_artifact_count)
    gauge("tuva_postgres_last_bytes_downloaded", "Bytes downloaded in the last successful run.", metrics.last_bytes_downloaded)
    gauge("tuva_postgres_last_rows_loaded_total", "Total rows loaded across all tables in the last successful run.", metrics.last_rows_loaded_total)
    gauge("tuva_postgres_last_test_failures", "Data-quality test failures in the last successful run.", metrics.last_test_failures)
    gauge("tuva_postgres_consecutive_failures", "Number of consecutive failed runs since the last success.", metrics.consecutive_failures)

    return "\n".join(lines) + "\n"


def write_prometheus_textfile(metrics: PipelineMetrics, path: Path) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_prometheus_textfile(metrics)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
