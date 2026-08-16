"""Standard-library unit tests for tuva_ingest.quarantine.

No real PostgreSQL connection required: a minimal fake connection/cursor
records every executed statement and its parameters (same pattern
test_state.py uses), so these tests verify (a) `ops_schema` is validated
before any SQL is composed, (b) the record is bound as an ordinary
parameter (never interpolated into SQL text), (c) the statement includes
the idempotency-guaranteeing `ON CONFLICT ... DO NOTHING` clause, and
(d) the returned fingerprint matches `record_fingerprint`. Full
round-trip behavior (a real INSERT, the restricted grants) is covered by
tests/integration/test_pipeline_integration.py.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.identifiers import InvalidIdentifierError  # noqa: E402
from tuva_ingest.quarantine import insert_quarantine_record, record_fingerprint  # noqa: E402
from tuva_ingest.validators import QuarantineDecision  # noqa: E402


class _FakeCursor:
    def __init__(self, log: list[tuple[str, tuple]]):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._log.append((sql, params))


class _FakeConnection:
    def __init__(self):
        self.log: list[tuple[str, tuple]] = []

    def cursor(self):
        return _FakeCursor(self.log)


class TestRecordFingerprint(unittest.TestCase):
    def test_deterministic_for_identical_content(self):
        record = {"b": 2, "a": 1}
        self.assertEqual(record_fingerprint(record), record_fingerprint({"a": 1, "b": 2}))

    def test_different_content_yields_different_fingerprint(self):
        self.assertNotEqual(record_fingerprint({"a": 1}), record_fingerprint({"a": 2}))

    def test_is_a_sha256_hex_digest(self):
        fp = record_fingerprint({"a": 1})
        self.assertEqual(len(fp), 64)
        int(fp, 16)  # raises ValueError if not valid hex

    def test_non_reversible_shape_never_contains_raw_field_values(self):
        fp = record_fingerprint({"ssn": "123-45-6789"})
        self.assertNotIn("123-45-6789", fp)


class TestInsertQuarantineRecordIdentifierValidation(unittest.TestCase):
    def test_invalid_ops_schema_raises_before_any_sql(self):
        conn = _FakeConnection()
        decision = QuarantineDecision("missing_required_field", "field 'person_id' is missing")
        with self.assertRaises(InvalidIdentifierError):
            insert_quarantine_record(
                conn, "bad; schema", run_id="r1", source="tuva", endpoint="eligibility",
                page_number=1, record_index=1, decision=decision, record={"a": 1},
            )
        self.assertEqual(conn.log, [])


class TestInsertQuarantineRecordStatement(unittest.TestCase):
    def test_statement_uses_on_conflict_do_nothing(self):
        conn = _FakeConnection()
        decision = QuarantineDecision("missing_required_field", "field 'person_id' is missing")
        insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="eligibility",
            page_number=1, record_index=1, decision=decision, record={"a": 1},
        )
        sql, _params = conn.log[0]
        self.assertIn("ON CONFLICT (run_id, page_number, record_index) DO NOTHING", sql)

    def test_qualified_relation_used_never_raw_schema_name(self):
        conn = _FakeConnection()
        decision = QuarantineDecision("missing_required_field", "x")
        insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="eligibility",
            page_number=1, record_index=1, decision=decision, record={"a": 1},
        )
        sql, _params = conn.log[0]
        self.assertIn('"ingest_ops"."quarantined_records"', sql)

    def test_record_bound_as_parameter_never_interpolated_into_sql_text(self):
        conn = _FakeConnection()
        decision = QuarantineDecision("missing_required_field", "x")
        sentinel = "UNIQUE-SENTINEL-VALUE-should-never-appear-in-sql-text"
        insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="eligibility",
            page_number=1, record_index=1, decision=decision, record={"note": sentinel},
        )
        sql, params = conn.log[0]
        self.assertNotIn(sentinel, sql)
        self.assertIn(sentinel, json.dumps(params, default=str))

    def test_returns_the_record_fingerprint(self):
        conn = _FakeConnection()
        decision = QuarantineDecision("missing_required_field", "x")
        record = {"a": 1}
        fingerprint = insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="eligibility",
            page_number=1, record_index=1, decision=decision, record=record,
        )
        self.assertEqual(fingerprint, record_fingerprint(record))

    def test_reason_code_and_bounded_detail_are_parameters(self):
        conn = _FakeConnection()
        decision = QuarantineDecision("invalid_date_format", "field 'claim_start_date' is not date-shaped")
        insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="medical_claim",
            page_number=2, record_index=5, decision=decision, record={"a": 1},
        )
        _sql, params = conn.log[0]
        self.assertIn("invalid_date_format", params)
        self.assertIn("field 'claim_start_date' is not date-shaped", params)

    def test_reason_detail_is_truncated_when_too_long(self):
        conn = _FakeConnection()
        long_detail = "x" * 500
        decision = QuarantineDecision("schema_validation_failed", long_detail)
        insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="eligibility",
            page_number=1, record_index=1, decision=decision, record={"a": 1},
        )
        _sql, params = conn.log[0]
        stored_detail = next(p for p in params if isinstance(p, str) and p.startswith("xxx"))
        self.assertLessEqual(len(stored_detail), 200)

    def test_non_dict_record_still_inserts_via_json_default_str(self):
        # A record that itself failed record_not_object (e.g. a bare
        # string) must still be quarantine-able -- json.dumps(...,
        # default=str) handles anything JSON-incompatible defensively.
        conn = _FakeConnection()
        decision = QuarantineDecision("record_not_object", "expected a JSON object, got str")
        insert_quarantine_record(
            conn, "ingest_ops", run_id="r1", source="tuva", endpoint="eligibility",
            page_number=1, record_index=1, decision=decision, record="not-a-dict",
        )
        _sql, params = conn.log[0]
        self.assertIn(json.dumps("not-a-dict", default=str), params)


if __name__ == "__main__":
    unittest.main()
