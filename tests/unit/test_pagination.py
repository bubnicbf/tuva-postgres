"""Standard-library unit tests for tuva_ingest.pagination: envelope
validation, immutable gzip-JSONL page files + run manifest, and the
one-page-at-a-time extraction orchestration. Every test uses a fake
ApiClient stand-in (never httpx.MockTransport/a real server -- HTTP-level
retry/auth/content-type/size-limit behavior is covered generically by
test_api_client.py, which exercises the same underlying
`_request_with_retries` machinery `get_json_page` uses) and a temporary
directory (never a real filesystem location outside pytest's tmp
handling). No network, no database.
"""
from __future__ import annotations

import gzip
import json
import logging
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import PaginationError  # noqa: E402
from tuva_ingest.pagination import (  # noqa: E402
    PageEnvelope,
    PaginatedRunStore,
    extract_paginated_run,
    file_sha256,
    validate_page_envelope,
)

_NULL_LOGGER = logging.getLogger("tuva_ingest.tests.test_pagination")
_NULL_LOGGER.addHandler(logging.NullHandler())
_NULL_LOGGER.propagate = False


def _page(records, *, page_token=None, next_page_token=None, high_water_mark="2025-01-02T00:00:00Z", record_count=None):
    return {
        "records": records,
        "metadata": {
            "record_count": len(records) if record_count is None else record_count,
            "page_token": page_token,
            "next_page_token": next_page_token,
            "high_water_mark": high_water_mark,
        },
    }


class TestValidatePageEnvelope(unittest.TestCase):
    def test_valid_single_page_envelope(self):
        payload = _page([{"a": 1}, {"a": 2}], next_page_token=None)
        envelope = validate_page_envelope(payload, requested_page_token=None)
        self.assertEqual(envelope.record_count, 2)
        self.assertIsNone(envelope.next_page_token)
        self.assertEqual(envelope.high_water_mark, "2025-01-02T00:00:00Z")

    def test_envelope_must_be_a_json_object(self):
        with self.assertRaises(PaginationError):
            validate_page_envelope([1, 2, 3], requested_page_token=None)

    def test_missing_records_field(self):
        payload = {"metadata": {"record_count": 0, "high_water_mark": "x"}}
        with self.assertRaises(PaginationError) as ctx:
            validate_page_envelope(payload, requested_page_token=None)
        self.assertIn("records", str(ctx.exception))

    def test_records_must_be_an_array(self):
        payload = {"records": {"not": "a list"}, "metadata": {"record_count": 0, "high_water_mark": "x"}}
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_record_must_be_an_object(self):
        payload = {"records": ["not-an-object"], "metadata": {"record_count": 1, "high_water_mark": "x"}}
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_missing_metadata_field(self):
        payload = {"records": []}
        with self.assertRaises(PaginationError) as ctx:
            validate_page_envelope(payload, requested_page_token=None)
        self.assertIn("metadata", str(ctx.exception))

    def test_metadata_must_be_an_object(self):
        payload = {"records": [], "metadata": "not-an-object"}
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_missing_record_count(self):
        payload = {"records": [], "metadata": {"high_water_mark": "x"}}
        with self.assertRaises(PaginationError) as ctx:
            validate_page_envelope(payload, requested_page_token=None)
        self.assertIn("record_count", str(ctx.exception))

    def test_record_count_must_be_non_negative_integer(self):
        for bad in (-1, "1", 1.5, True):
            with self.subTest(bad=bad):
                payload = {"records": [], "metadata": {"record_count": bad, "high_water_mark": "x"}}
                with self.assertRaises(PaginationError):
                    validate_page_envelope(payload, requested_page_token=None)

    def test_record_count_mismatch(self):
        payload = _page([{"a": 1}], record_count=5)
        with self.assertRaises(PaginationError) as ctx:
            validate_page_envelope(payload, requested_page_token=None)
        self.assertIn("does not match", str(ctx.exception))

    def test_returned_page_token_matches_requested(self):
        payload = _page([{"a": 1}], page_token="tok-2")
        envelope = validate_page_envelope(payload, requested_page_token="tok-2")
        self.assertEqual(envelope.page_token, "tok-2")

    def test_returned_page_token_mismatch_rejected(self):
        payload = _page([{"a": 1}], page_token="tok-WRONG")
        with self.assertRaises(PaginationError) as ctx:
            validate_page_envelope(payload, requested_page_token="tok-2")
        self.assertIn("does not match the requested", str(ctx.exception))

    def test_no_token_check_when_requested_token_is_none(self):
        # Page 1 has no requested token -- an echoed page_token (or None)
        # is never compared to anything.
        payload = _page([{"a": 1}], page_token="anything")
        validate_page_envelope(payload, requested_page_token=None)  # must not raise

    def test_next_page_token_null_means_final_page(self):
        payload = _page([{"a": 1}], next_page_token=None)
        envelope = validate_page_envelope(payload, requested_page_token=None)
        self.assertIsNone(envelope.next_page_token)

    def test_next_page_token_absent_means_final_page(self):
        payload = {
            "records": [{"a": 1}],
            "metadata": {"record_count": 1, "high_water_mark": "x"},  # next_page_token absent entirely
        }
        envelope = validate_page_envelope(payload, requested_page_token=None)
        self.assertIsNone(envelope.next_page_token)

    def test_next_page_token_empty_string_rejected(self):
        payload = _page([{"a": 1}], next_page_token="")
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_next_page_token_non_string_rejected(self):
        payload = _page([{"a": 1}], next_page_token=42)
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_missing_high_water_mark_rejected(self):
        payload = {"records": [], "metadata": {"record_count": 0}}
        with self.assertRaises(PaginationError) as ctx:
            validate_page_envelope(payload, requested_page_token=None)
        self.assertIn("high_water_mark", str(ctx.exception))

    def test_empty_high_water_mark_rejected(self):
        payload = _page([], high_water_mark="")
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_null_high_water_mark_rejected(self):
        payload = _page([], high_water_mark=None)
        with self.assertRaises(PaginationError):
            validate_page_envelope(payload, requested_page_token=None)

    def test_records_values_and_structure_preserved_exactly(self):
        record = {"b": 2, "a": 1, "nested": {"x": [1, None, "y"]}, "null_field": None}
        payload = _page([record])
        envelope = validate_page_envelope(payload, requested_page_token=None)
        self.assertEqual(envelope.records[0], record)
        # Key order/nulls are exactly as given -- no re-ordering, no
        # null-stripping happens during validation.
        self.assertIn("null_field", envelope.records[0])


class TestPaginatedRunStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PaginatedRunStore(Path(self._tmp.name), "tuva")

    def _envelope(self, records, **kwargs):
        payload = _page(records, **kwargs)
        return validate_page_envelope(payload, requested_page_token=kwargs.get("_requested"))

    def test_write_page_produces_one_gzip_jsonl_file(self):
        staging = self.store.begin_staging("run-1")
        envelope = self._envelope([{"a": 1}, {"a": 2}])
        from datetime import datetime, timezone

        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        page_path = staging / meta.file_name
        self.assertTrue(page_path.is_file())
        self.assertTrue(meta.file_name.endswith(".jsonl.gz"))
        self.assertFalse((staging / f"{meta.file_name}.part").exists())

    def test_decompressed_records_match_exactly(self):
        staging = self.store.begin_staging("run-1")
        records = [{"claim_id": "c1", "amount": 12.5}, {"claim_id": "c2", "amount": None}]
        envelope = self._envelope(records)
        from datetime import datetime, timezone

        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        with gzip.open(staging / meta.file_name, "rt", encoding="utf-8") as fh:
            decoded = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(decoded, records)

    def test_sha256_is_over_the_stored_compressed_bytes(self):
        staging = self.store.begin_staging("run-1")
        envelope = self._envelope([{"a": 1}])
        from datetime import datetime, timezone

        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        page_path = staging / meta.file_name
        self.assertEqual(meta.sha256, file_sha256(page_path))
        self.assertEqual(meta.compressed_size_bytes, page_path.stat().st_size)

    def test_deterministic_output_for_identical_input(self):
        from datetime import datetime, timezone

        retrieved_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        envelope = self._envelope([{"a": 1}, {"b": 2}])

        staging1 = self.store.begin_staging("run-a")
        meta1 = self.store.write_page(
            staging1, run_id="run-a", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=retrieved_at,
        )
        staging2 = self.store.begin_staging("run-b")
        meta2 = self.store.write_page(
            staging2, run_id="run-a", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=retrieved_at,
        )
        self.assertEqual((staging1 / meta1.file_name).read_bytes(), (staging2 / meta2.file_name).read_bytes())
        self.assertEqual(meta1.sha256, meta2.sha256)

    def test_finalize_writes_success_marker_and_manifest(self):
        staging = self.store.begin_staging("run-1")
        from datetime import datetime, timezone

        envelope = self._envelope([{"a": 1}])
        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        published = self.store.finalize(
            staging, "run-1", [meta], endpoint="eligibility", since=None,
            total_record_count=1, candidate_high_water_mark="2025-01-02T00:00:00Z",
        )
        self.assertTrue((published / "_SUCCESS").is_file())
        self.assertTrue((published / "manifest.json").is_file())
        self.assertTrue(self.store.is_published("run-1"))
        manifest = self.store.read_manifest("run-1")
        self.assertEqual(manifest["total_record_count"], 1)
        self.assertEqual(manifest["candidate_high_water_mark"], "2025-01-02T00:00:00Z")
        self.assertEqual(len(manifest["pages"]), 1)

    def test_finalize_refuses_to_overwrite_a_published_run(self):
        staging = self.store.begin_staging("run-1")
        from datetime import datetime, timezone

        envelope = self._envelope([{"a": 1}])
        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        self.store.finalize(
            staging, "run-1", [meta], endpoint="eligibility", since=None,
            total_record_count=1, candidate_high_water_mark="x",
        )
        staging2 = self.store.begin_staging("run-1")
        with self.assertRaises(PaginationError):
            self.store.finalize(
                staging2, "run-1", [meta], endpoint="eligibility", since=None,
                total_record_count=1, candidate_high_water_mark="x",
            )

    def test_abort_staging_removes_partial_files(self):
        staging = self.store.begin_staging("run-1")
        (staging / "partial.jsonl.gz.part").write_bytes(b"junk")
        self.store.abort_staging(staging)
        self.assertFalse(staging.exists())

    def test_check_existing_run_returns_none_when_not_published(self):
        self.assertIsNone(self.store.check_existing_run("does-not-exist"))

    def test_check_existing_run_returns_manifest_for_intact_run(self):
        staging = self.store.begin_staging("run-1")
        from datetime import datetime, timezone

        envelope = self._envelope([{"a": 1}])
        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        self.store.finalize(
            staging, "run-1", [meta], endpoint="eligibility", since=None,
            total_record_count=1, candidate_high_water_mark="x",
        )
        existing = self.store.check_existing_run("run-1")
        self.assertIsNotNone(existing)
        self.assertEqual(existing["run_id"], "run-1")

    def test_check_existing_run_rejects_corrupted_page_file(self):
        staging = self.store.begin_staging("run-1")
        from datetime import datetime, timezone

        envelope = self._envelope([{"a": 1}])
        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        published = self.store.finalize(
            staging, "run-1", [meta], endpoint="eligibility", since=None,
            total_record_count=1, candidate_high_water_mark="x",
        )
        (published / meta.file_name).write_bytes(b"tampered-bytes")
        with self.assertRaises(PaginationError):
            self.store.check_existing_run("run-1")

    def test_check_existing_run_rejects_missing_page_file(self):
        staging = self.store.begin_staging("run-1")
        from datetime import datetime, timezone

        envelope = self._envelope([{"a": 1}])
        meta = self.store.write_page(
            staging, run_id="run-1", endpoint="eligibility", page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        published = self.store.finalize(
            staging, "run-1", [meta], endpoint="eligibility", since=None,
            total_record_count=1, candidate_high_water_mark="x",
        )
        (published / meta.file_name).unlink()
        with self.assertRaises(PaginationError):
            self.store.check_existing_run("run-1")


@dataclass
class _FakeConfig:
    api_manifest_url: str
    raw_data_dir: Path
    source_name: str
    api_max_pages: int = 100
    api_page_size: int | None = None
    api_max_page_bytes: int = 64 * 1024 * 1024


class _FakePaginationClient:
    """A minimal stand-in for ApiClient used only by
    `extract_paginated_run` -- exposes exactly the one method it calls,
    `get_json_page`, and records every call's params so tests can assert
    endpoint/since/page_token/page_size are passed as real parameters."""

    def __init__(self, pages: list[dict]):
        self._pages = list(pages)
        self.calls: list[dict] = []

    def get_json_page(self, url, *, params=None, max_bytes=None):
        self.calls.append(dict(params or {}))
        if not self._pages:
            raise AssertionError("no more scripted pages")
        return self._pages.pop(0)


class TestExtractPaginatedRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)

    def _config(self, **kwargs):
        return _FakeConfig(
            api_manifest_url="https://example.invalid/v1/records",
            raw_data_dir=self.raw_data_dir,
            source_name="tuva",
            **kwargs,
        )

    def test_single_page_extraction(self):
        client = _FakePaginationClient([_page([{"a": 1}, {"a": 2}], next_page_token=None, high_water_mark="hwm-1")])
        result = extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertFalse(result.skipped)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.total_record_count, 2)
        self.assertEqual(result.candidate_high_water_mark, "hwm-1")
        self.assertEqual(result.table, "eligibility")
        self.assertTrue((result.path / "_SUCCESS").is_file())

    def test_multi_page_extraction_and_next_page_token_propagation(self):
        client = _FakePaginationClient([
            _page([{"a": 1}], next_page_token="tok-2", high_water_mark="hwm-1"),
            _page([{"a": 2}], page_token="tok-2", next_page_token="tok-3", high_water_mark="hwm-2"),
            _page([{"a": 3}], page_token="tok-3", next_page_token=None, high_water_mark="hwm-3"),
        ])
        result = extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="medical-claims")
        self.assertEqual(result.page_count, 3)
        self.assertEqual(result.total_record_count, 3)
        # Deterministic selection: the LAST page's candidate wins.
        self.assertEqual(result.candidate_high_water_mark, "hwm-3")
        # page_token was correctly threaded from each page's next_page_token
        # into the following request.
        self.assertEqual(client.calls[0].get("page_token"), None)
        self.assertEqual(client.calls[1]["page_token"], "tok-2")
        self.assertEqual(client.calls[2]["page_token"], "tok-3")

    def test_since_and_endpoint_passed_as_params(self):
        client = _FakePaginationClient([_page([], next_page_token=None)])
        extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility", since="2025-01-01")
        self.assertEqual(client.calls[0]["endpoint"], "eligibility")
        self.assertEqual(client.calls[0]["since"], "2025-01-01")

    def test_since_omitted_when_not_provided(self):
        client = _FakePaginationClient([_page([], next_page_token=None)])
        extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertNotIn("since", client.calls[0])

    def test_page_size_passed_when_configured(self):
        client = _FakePaginationClient([_page([], next_page_token=None)])
        extract_paginated_run(self._config(api_page_size=50), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertEqual(client.calls[0]["page_size"], "50")

    def test_final_page_detected_on_null_next_page_token(self):
        client = _FakePaginationClient([_page([{"a": 1}], next_page_token=None)])
        result = extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertEqual(result.page_count, 1)

    def test_missing_records_field_aborts_and_cleans_staging(self):
        client = _FakePaginationClient([{"metadata": {"record_count": 0, "high_water_mark": "x"}}])
        with self.assertRaises(PaginationError):
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        store = PaginatedRunStore(self.raw_data_dir, "tuva")
        staging_root = store._staging_root()
        if staging_root.exists():
            self.assertEqual(list(staging_root.iterdir()), [])

    def test_invalid_metadata_type_aborts_extraction(self):
        client = _FakePaginationClient([{"records": [], "metadata": "not-an-object"}])
        with self.assertRaises(PaginationError):
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")

    def test_record_count_mismatch_aborts_extraction(self):
        client = _FakePaginationClient([_page([{"a": 1}], record_count=99)])
        with self.assertRaises(PaginationError):
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")

    def test_returned_token_mismatch_aborts_extraction(self):
        client = _FakePaginationClient([
            _page([{"a": 1}], next_page_token="tok-2"),
            _page([{"a": 2}], page_token="tok-WRONG", next_page_token=None),
        ])
        with self.assertRaises(PaginationError):
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")

    def test_repeated_next_page_token_is_a_pagination_cycle(self):
        client = _FakePaginationClient([
            _page([{"a": 1}], next_page_token="tok-2"),
            _page([{"a": 2}], page_token="tok-2", next_page_token="tok-2"),  # repeats itself
        ])
        with self.assertRaises(PaginationError) as ctx:
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertIn("cycle", str(ctx.exception))

    def test_next_page_token_equal_to_a_prior_request_token_is_a_cycle(self):
        client = _FakePaginationClient([
            _page([{"a": 1}], next_page_token="tok-2"),
            _page([{"a": 2}], page_token="tok-2", next_page_token="tok-3"),
            _page([{"a": 3}], page_token="tok-3", next_page_token="tok-2"),  # back to page 2's token
        ])
        with self.assertRaises(PaginationError):
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")

    def test_max_page_limit_enforced(self):
        pages = [_page([{"a": i}], page_token=(f"tok-{i}" if i else None), next_page_token=f"tok-{i + 1}") for i in range(5)]
        client = _FakePaginationClient(pages)
        with self.assertRaises(PaginationError) as ctx:
            extract_paginated_run(self._config(api_max_pages=3), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertIn("maximum", str(ctx.exception))

    def test_unsupported_endpoint_rejected_before_any_request(self):
        client = _FakePaginationClient([_page([], next_page_token=None)])
        with self.assertRaises(Exception):
            extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="not-a-real-endpoint")
        self.assertEqual(client.calls, [])

    def test_page_files_are_published_atomically_and_immutably(self):
        client = _FakePaginationClient([_page([{"a": 1}], next_page_token=None)])
        result = extract_paginated_run(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        page_files = list(result.path.glob("page-*.jsonl.gz"))
        self.assertEqual(len(page_files), 1)
        self.assertTrue((result.path / "_SUCCESS").is_file())


if __name__ == "__main__":
    unittest.main()
