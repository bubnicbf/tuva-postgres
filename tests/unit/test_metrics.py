"""Unit tests for tuva_postgres.metrics: Prometheus textfile rendering
(pure string formatting) and atomic-write behavior, plus compute_metrics
against a fake ops module (no real PostgreSQL needed)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres import metrics  # noqa: E402


class TestRenderPrometheusTextfile(unittest.TestCase):
    def test_render_includes_all_expected_metric_names(self):
        m = metrics.PipelineMetrics(
            last_success_timestamp=1234.0, last_run_status="succeeded", last_run_duration_seconds=12.5,
            last_artifact_count=15, last_bytes_downloaded=1000, last_rows_loaded_total=500,
            last_test_failures=0, consecutive_failures=0,
        )
        text = metrics.render_prometheus_textfile(m)
        for name in (
            "tuva_postgres_last_success_timestamp_seconds",
            "tuva_postgres_last_run_status",
            "tuva_postgres_last_run_duration_seconds",
            "tuva_postgres_last_artifact_count",
            "tuva_postgres_last_bytes_downloaded",
            "tuva_postgres_last_rows_loaded_total",
            "tuva_postgres_last_test_failures",
            "tuva_postgres_consecutive_failures",
        ):
            self.assertIn(name, text)
        self.assertIn('tuva_postgres_last_run_status{status="succeeded"} 1', text)
        self.assertIn('tuva_postgres_last_run_status{status="failed"} 0', text)

    def test_render_handles_none_values_as_zero(self):
        m = metrics.PipelineMetrics(
            last_success_timestamp=None, last_run_status=None, last_run_duration_seconds=None,
            last_artifact_count=None, last_bytes_downloaded=None, last_rows_loaded_total=None,
            last_test_failures=None, consecutive_failures=0,
        )
        text = metrics.render_prometheus_textfile(m)
        self.assertIn("tuva_postgres_last_success_timestamp_seconds 0", text)


class TestWriteAtomic(unittest.TestCase):
    def test_write_is_atomic_and_readable(self):
        m = metrics.PipelineMetrics(
            last_success_timestamp=1.0, last_run_status="succeeded", last_run_duration_seconds=1.0,
            last_artifact_count=1, last_bytes_downloaded=1, last_rows_loaded_total=1,
            last_test_failures=0, consecutive_failures=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "metrics.prom"
            metrics.write_prometheus_textfile(m, target)
            self.assertTrue(target.is_file())
            content = target.read_text()
            self.assertIn("tuva_postgres_consecutive_failures 0", content)
            leftover_tmp_files = list(target.parent.glob(".*.tmp-*"))
            self.assertEqual(leftover_tmp_files, [])


class TestComputeMetrics(unittest.TestCase):
    def test_compute_metrics_uses_ops_module(self):
        finished_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        fake_last = ("r1", "succeeded", None, None, "load", None)
        fake_last_success = ("r1", finished_at, 15, 12345, '{"patient": 10, "encounter": 5}', 3, 0)

        with mock.patch.object(metrics.ops, "latest_run", return_value=fake_last), \
             mock.patch.object(metrics.ops, "latest_successful_run", return_value=fake_last_success), \
             mock.patch.object(metrics.ops, "consecutive_failures", return_value=0):
            result = metrics.compute_metrics(conn=object(), ops_schema="tuva_ops")

        self.assertEqual(result.last_run_status, "succeeded")
        self.assertEqual(result.last_artifact_count, 15)
        self.assertEqual(result.last_rows_loaded_total, 15)
        self.assertEqual(result.consecutive_failures, 0)
        self.assertAlmostEqual(result.last_success_timestamp, finished_at.timestamp())

    def test_compute_metrics_handles_no_history(self):
        with mock.patch.object(metrics.ops, "latest_run", return_value=None), \
             mock.patch.object(metrics.ops, "latest_successful_run", return_value=None), \
             mock.patch.object(metrics.ops, "consecutive_failures", return_value=0):
            result = metrics.compute_metrics(conn=object(), ops_schema="tuva_ops")
        self.assertIsNone(result.last_run_status)
        self.assertIsNone(result.last_success_timestamp)


if __name__ == "__main__":
    unittest.main()
