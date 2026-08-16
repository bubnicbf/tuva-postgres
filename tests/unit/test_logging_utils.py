"""Standard-library unit tests for tuva_ingest.logging_utils: structured
JSON logging (one valid JSON object per line) and secret redaction.
Database-free, network-free.
"""
from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.logging_utils import configure_logging, log_event, sanitize_error, sanitize_text  # noqa: E402


class TestSanitizeText(unittest.TestCase):
    def test_bearer_token_redacted(self):
        text = "request failed: Authorization: Bearer abc123.def-456_ghi"
        self.assertNotIn("abc123.def-456_ghi", sanitize_text(text))
        self.assertIn("***REDACTED***", sanitize_text(text))

    def test_dsn_password_redacted(self):
        text = "connection failed for postgresql://myuser:s3cr3t-pass@db.internal:5432/tuva"
        sanitized = sanitize_text(text)
        self.assertNotIn("s3cr3t-pass", sanitized)
        self.assertIn("myuser", sanitized)  # username is not a secret

    def test_dsn_password_redacted_postgres_scheme(self):
        text = "postgres://u:hunter2@localhost/db"
        self.assertNotIn("hunter2", sanitize_text(text))

    def test_plain_text_passed_through_unchanged(self):
        text = "manifest fetch failed with HTTP 503"
        self.assertEqual(sanitize_text(text), text)

    def test_empty_string_passed_through(self):
        self.assertEqual(sanitize_text(""), "")


class TestSanitizeError(unittest.TestCase):
    def test_uses_the_exception_category_attribute(self):
        class _Fake(Exception):
            category = "download"

        category, message = sanitize_error(_Fake("boom"))
        self.assertEqual(category, "download")
        self.assertEqual(message, "boom")

    def test_falls_back_to_class_name_without_category(self):
        category, _message = sanitize_error(ValueError("x"))
        self.assertEqual(category, "ValueError")

    def test_never_includes_a_traceback(self):
        try:
            raise RuntimeError("Authorization: Bearer super-secret-token")
        except RuntimeError as exc:
            _category, message = sanitize_error(exc)
        self.assertNotIn("super-secret-token", message)
        self.assertNotIn("Traceback", message)


class TestJsonFormatterEmitsValidJsonPerLine(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.logger = configure_logging("INFO", stream=self.stream)
        self.addCleanup(self.logger.handlers.clear)

    def test_single_log_event_is_one_valid_json_object(self):
        log_event(self.logger, "extract_started", run_id="r1", endpoint="eligibility")
        lines = [line for line in self.stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])  # must not raise
        self.assertEqual(payload["event"], "extract_started")
        self.assertEqual(payload["run_id"], "r1")
        self.assertEqual(payload["endpoint"], "eligibility")

    def test_every_line_across_multiple_events_is_independently_valid_json(self):
        log_event(self.logger, "extract_started", run_id="r1", endpoint="medical-claims")
        log_event(self.logger, "artifact_download_completed", run_id="r1", table="medical_claim", duration_ms=12.345)
        log_event(self.logger, "extract_succeeded", run_id="r1", status="succeeded")
        lines = [line for line in self.stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            json.loads(line)  # must not raise for any line

    def test_required_fields_present(self):
        log_event(self.logger, "raw_table_loaded", run_id="r1", endpoint="eligibility", stage="load", duration_ms=5.0)
        payload = json.loads(self.stream.getvalue().splitlines()[0])
        for field in ("event", "level", "app_version", "timestamp", "run_id", "endpoint", "stage", "duration_ms"):
            self.assertIn(field, payload)

    def test_error_category_and_message_included_when_present(self):
        log_event(self.logger, "load_failed", run_id="r1", error_category="raw_load", error_message="checksum mismatch")
        payload = json.loads(self.stream.getvalue().splitlines()[0])
        self.assertEqual(payload["error_category"], "raw_load")
        self.assertEqual(payload["error_message"], "checksum mismatch")

    def test_string_field_values_are_sanitized(self):
        log_event(self.logger, "manifest_fetch_failed", run_id="r1", error_message="Authorization: Bearer abc.def-ghi")
        payload = json.loads(self.stream.getvalue().splitlines()[0])
        self.assertNotIn("abc.def-ghi", payload["error_message"])

    def test_timestamp_is_utc_iso8601(self):
        log_event(self.logger, "x")
        payload = json.loads(self.stream.getvalue().splitlines()[0])
        self.assertTrue(payload["timestamp"].endswith("Z"))
        # Must parse as an ISO-8601-ish timestamp (fromisoformat needs
        # the trailing Z translated to +00:00, mirroring manifest.py's
        # own timestamp parsing convention).
        from datetime import datetime

        datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))

    def test_configure_logging_defaults_to_stdout(self):
        logger = configure_logging("INFO")
        try:
            handler = logger.handlers[0]
            self.assertIs(handler.stream, sys.stdout)
        finally:
            logger.handlers.clear()

    def test_logger_does_not_propagate_to_root(self):
        self.assertFalse(self.logger.propagate)


if __name__ == "__main__":
    unittest.main()
