"""Standard-library unit tests for tuva_ingest.extract.

Pure filesystem logic for RawSnapshotStore (no network or database
required); extract_snapshot() orchestration is tested against a small
fake ApiClient stand-in rather than a real HTTP server.
"""
from __future__ import annotations

import json
import logging
import stat
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import ExtractError  # noqa: E402
from tuva_ingest.extract import RawSnapshotStore, extract_endpoint_snapshot, extract_snapshot  # noqa: E402
from tuva_ingest.manifest import RAW_TABLES  # noqa: E402

# extract_snapshot() logs structured events through a real
# logging.Logger (see logging_utils.log_event) -- a null handler keeps
# these tests quiet without needing a fake/None logger stand-in.
_NULL_LOGGER = logging.getLogger("tuva_ingest.tests.test_extract")
_NULL_LOGGER.addHandler(logging.NullHandler())
_NULL_LOGGER.propagate = False


def _manifest(snapshot_id="snap-1"):
    return {"version": 1, "source": "tuva", "snapshot_id": snapshot_id, "created_at": "2026-08-14T06:00:00Z", "artifacts": []}


class TestRawSnapshotStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RawSnapshotStore(Path(self._tmp.name), "tuva")

    def _publish_snapshot(self, snapshot_id="snap-1", manifest=None):
        manifest = manifest or _manifest(snapshot_id)
        staging = self.store.begin_staging(snapshot_id)
        (staging / "eligibility.csv").write_text("patient_id\n1\n", encoding="utf-8")
        checksums = {"eligibility": {"sha256": "a" * 64, "size_bytes": 5}}
        return self.store.finalize(staging, snapshot_id, manifest, checksums)

    def test_atomic_publication_and_success_marker(self):
        published = self._publish_snapshot()
        self.assertTrue((published.path / "_SUCCESS").is_file())
        self.assertTrue((published.path / "manifest.json").is_file())
        self.assertTrue((published.path / "checksums.json").is_file())
        self.assertTrue((published.path / "eligibility.csv").is_file())
        self.assertTrue(self.store.is_published("snap-1"))

    def test_current_pointer_advances_only_after_success(self):
        self.assertIsNone(self.store.current_snapshot_id())
        self._publish_snapshot("snap-1")
        self.assertEqual(self.store.current_snapshot_id(), "snap-1")
        self._publish_snapshot("snap-2")
        self.assertEqual(self.store.current_snapshot_id(), "snap-2")

    def test_current_pointer_not_advanced_on_aborted_staging(self):
        self._publish_snapshot("snap-1")
        staging = self.store.begin_staging("snap-2")
        (staging / "eligibility.csv").write_text("bad", encoding="utf-8")
        self.store.abort_staging(staging)
        self.assertEqual(self.store.current_snapshot_id(), "snap-1")
        self.assertFalse(self.store.is_published("snap-2"))
        self.assertFalse(staging.exists())

    def test_identical_snapshot_is_idempotent(self):
        manifest = _manifest("snap-1")
        self._publish_snapshot("snap-1", manifest)
        skip = self.store.check_idempotent_or_conflicting("snap-1", manifest)
        self.assertTrue(skip)

    def test_conflicting_snapshot_content_fails_loudly(self):
        self._publish_snapshot("snap-1", _manifest("snap-1"))
        different = _manifest("snap-1")
        different["created_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(ExtractError):
            self.store.check_idempotent_or_conflicting("snap-1", different)

    def test_finalize_refuses_to_overwrite_completed_snapshot(self):
        self._publish_snapshot("snap-1")
        staging = self.store.begin_staging("snap-1")
        with self.assertRaises(ExtractError):
            self.store.finalize(staging, "snap-1", _manifest("snap-1"), {})

    def test_restrictive_permissions(self):
        published = self._publish_snapshot()
        mode = stat.S_IMODE(published.path.stat().st_mode)
        self.assertEqual(mode, 0o750)
        file_mode = stat.S_IMODE((published.path / "eligibility.csv").stat().st_mode)
        self.assertEqual(file_mode, 0o640)

    def test_no_staging_leftover_after_publish(self):
        self._publish_snapshot()
        staging_root = self.store._staging_root()
        self.assertEqual(list(staging_root.iterdir()), [])


@dataclass
class _DownloadResult:
    sha256: str
    size_bytes: int
    duration_ms: float = 1.0


class _FakeApiClient:
    """A minimal stand-in for ApiClient: no network. `fail_table`, if
    set, raises on the download for that one table so extract_snapshot's
    all-or-nothing staging cleanup can be tested without a real server."""

    def __init__(self, manifest_json: dict, fail_table: str | None = None):
        self._manifest_json = manifest_json
        self.fail_table = fail_table
        self.downloaded: list[str] = []

    def fetch_manifest_json(self, url: str) -> dict:
        return self._manifest_json

    def download_artifact(self, artifact, dest_dir: Path):
        if artifact.table == self.fail_table:
            raise RuntimeError(f"simulated download failure for {artifact.table}")
        (dest_dir / artifact.filename).write_text("col\nval\n", encoding="utf-8")
        self.downloaded.append(artifact.table)
        return _DownloadResult(sha256="a" * 64, size_bytes=8)


def _full_manifest(snapshot_id="snap-1") -> dict:
    return {
        "version": 1,
        "source": "tuva",
        "snapshot_id": snapshot_id,
        "created_at": "2026-08-14T06:00:00Z",
        "artifacts": [
            {
                "table": t,
                "url": f"https://example.invalid/{snapshot_id}/{t}.csv",
                "sha256": "a" * 64,
                "size_bytes": 8,
            }
            for t in RAW_TABLES
        ],
    }


@dataclass
class _FakeConfig:
    api_manifest_url: str
    api_allow_insecure_http: bool
    raw_data_dir: Path
    source_name: str


class TestExtractSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)

    def _config(self):
        return _FakeConfig(
            api_manifest_url="https://example.invalid/manifest.json",
            api_allow_insecure_http=False,
            raw_data_dir=self.raw_data_dir,
            source_name="tuva",
        )

    def test_successful_extraction_publishes_all_artifacts(self):
        manifest_json = _full_manifest()
        client = _FakeApiClient(manifest_json)
        result = extract_snapshot(self._config(), client, logger=_NULL_LOGGER)
        self.assertFalse(result.skipped)
        self.assertEqual(result.snapshot_id, "snap-1")
        for table in RAW_TABLES:
            self.assertTrue((result.path / f"{table}.csv").is_file())
        self.assertTrue((result.path / "_SUCCESS").is_file())
        self.assertEqual(sorted(client.downloaded), sorted(RAW_TABLES))

    def test_repeated_identical_extraction_is_skipped(self):
        manifest_json = _full_manifest()
        client = _FakeApiClient(manifest_json)
        first = extract_snapshot(self._config(), client, logger=_NULL_LOGGER)
        self.assertFalse(first.skipped)

        client2 = _FakeApiClient(manifest_json)
        second = extract_snapshot(self._config(), client2, logger=_NULL_LOGGER)
        self.assertTrue(second.skipped)
        self.assertEqual(second.snapshot_id, "snap-1")
        # No artifacts should have been re-downloaded on the skip path.
        self.assertEqual(client2.downloaded, [])

    def test_partial_download_never_appears_complete(self):
        manifest_json = _full_manifest()
        client = _FakeApiClient(manifest_json, fail_table="medical_claim")
        with self.assertRaises(RuntimeError):
            extract_snapshot(self._config(), client, logger=_NULL_LOGGER)

        store = RawSnapshotStore(self.raw_data_dir, "tuva")
        self.assertFalse(store.is_published("snap-1"))
        self.assertIsNone(store.current_snapshot_id())
        # No leftover staging directories after the failure is cleaned up.
        staging_root = store._staging_root()
        if staging_root.exists():
            self.assertEqual(list(staging_root.iterdir()), [])




def _endpoint_manifest(table: str, snapshot_id: str = "snap-ep-1") -> dict:
    return {
        "version": 1,
        "source": "tuva",
        "snapshot_id": snapshot_id,
        "created_at": "2026-08-14T06:00:00Z",
        "artifacts": [
            {
                "table": table,
                "url": f"https://example.invalid/{snapshot_id}/{table}.csv",
                "sha256": "a" * 64,
                "size_bytes": 8,
            }
        ],
    }


class _FakeEndpointApiClient:
    """A minimal stand-in for ApiClient used by extract_endpoint_snapshot:
    no network, records the query params it was called with so tests can
    assert endpoint/since are passed as real params (never string
    concatenation)."""

    def __init__(self, manifest_json: dict, fail: bool = False):
        self._manifest_json = manifest_json
        self.fail = fail
        self.downloaded: list[str] = []
        self.last_params: dict | None = None

    def fetch_manifest_json(self, url: str, params: dict | None = None) -> dict:
        self.last_params = params
        return self._manifest_json

    def download_artifact(self, artifact, dest_dir: Path):
        if self.fail:
            raise RuntimeError(f"simulated download failure for {artifact.table}")
        (dest_dir / artifact.filename).write_text("col\nval\n", encoding="utf-8")
        self.downloaded.append(artifact.table)
        return _DownloadResult(sha256="a" * 64, size_bytes=8)


class TestExtractEndpointSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)

    def _config(self):
        return _FakeConfig(
            api_manifest_url="https://example.invalid/manifest.json",
            api_allow_insecure_http=False,
            raw_data_dir=self.raw_data_dir,
            source_name="tuva",
        )

    def test_endpoint_is_mapped_to_its_table_and_published(self):
        manifest_json = _endpoint_manifest("eligibility")
        client = _FakeEndpointApiClient(manifest_json)
        result = extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertEqual(result.table, "eligibility")
        self.assertEqual(result.endpoint, "eligibility")
        self.assertFalse(result.skipped)
        self.assertTrue((result.path / "eligibility.csv").is_file())
        self.assertTrue((result.path / "_SUCCESS").is_file())

    def test_run_id_equals_manifest_snapshot_id(self):
        manifest_json = _endpoint_manifest("medical_claim", snapshot_id="snap-run-id-1")
        client = _FakeEndpointApiClient(manifest_json)
        result = extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="medical-claims")
        self.assertEqual(result.run_id, "snap-run-id-1")
        self.assertEqual(result.snapshot_id, "snap-run-id-1")

    def test_endpoint_and_since_sent_as_query_params_not_concatenated(self):
        manifest_json = _endpoint_manifest("pharmacy_claim")
        client = _FakeEndpointApiClient(manifest_json)
        extract_endpoint_snapshot(
            self._config(), client, _NULL_LOGGER, endpoint="pharmacy-claims", since="2025-01-01"
        )
        self.assertEqual(client.last_params, {"endpoint": "pharmacy-claims", "since": "2025-01-01"})

    def test_since_omitted_from_params_when_not_provided(self):
        manifest_json = _endpoint_manifest("eligibility")
        client = _FakeEndpointApiClient(manifest_json)
        extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertEqual(client.last_params, {"endpoint": "eligibility"})

    def test_requested_endpoint_and_since_persisted_in_manifest_json(self):
        manifest_json = _endpoint_manifest("eligibility")
        client = _FakeEndpointApiClient(manifest_json)
        result = extract_endpoint_snapshot(
            self._config(), client, _NULL_LOGGER, endpoint="eligibility", since="2025-06-01"
        )
        persisted = json.loads((result.path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["_requested_endpoint"], "eligibility")
        self.assertEqual(persisted["_requested_since"], "2025-06-01")

    def test_does_not_advance_legacy_current_pointer(self):
        manifest_json = _endpoint_manifest("eligibility")
        client = _FakeEndpointApiClient(manifest_json)
        extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        store = RawSnapshotStore(self.raw_data_dir, "tuva")
        self.assertIsNone(store.current_snapshot_id())

    def test_manifest_with_artifact_for_wrong_table_is_rejected(self):
        # A manifest containing an artifact for a table that wasn't
        # requested must never be silently accepted.
        manifest_json = _endpoint_manifest("medical_claim")
        client = _FakeEndpointApiClient(manifest_json)
        with self.assertRaises(Exception):
            extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="eligibility")

    def test_repeated_identical_endpoint_extraction_is_skipped(self):
        manifest_json = _endpoint_manifest("eligibility")
        client = _FakeEndpointApiClient(manifest_json)
        first = extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        self.assertFalse(first.skipped)

        client2 = _FakeEndpointApiClient(manifest_json)
        second = extract_endpoint_snapshot(self._config(), client2, _NULL_LOGGER, endpoint="eligibility")
        self.assertTrue(second.skipped)
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(client2.downloaded, [])

    def test_failed_download_leaves_no_partial_snapshot(self):
        manifest_json = _endpoint_manifest("eligibility")
        client = _FakeEndpointApiClient(manifest_json, fail=True)
        with self.assertRaises(RuntimeError):
            extract_endpoint_snapshot(self._config(), client, _NULL_LOGGER, endpoint="eligibility")
        store = RawSnapshotStore(self.raw_data_dir, "tuva")
        self.assertFalse(store.is_published("snap-ep-1"))
        staging_root = store._staging_root()
        if staging_root.exists():
            self.assertEqual(list(staging_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
