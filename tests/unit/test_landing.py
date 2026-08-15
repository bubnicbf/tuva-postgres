"""Standard-library unit tests for tuva_postgres.landing (RawLandingLayer).

Pure filesystem logic -- no network or database required.
"""
from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.errors import LandingError  # noqa: E402
from tuva_postgres.landing import RawLandingLayer  # noqa: E402


def _manifest(snapshot_id="snap-1"):
    return {"version": 1, "source": "tuva", "snapshot_id": snapshot_id, "created_at": "2026-08-14T06:00:00Z", "artifacts": []}


class TestRawLandingLayer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layer = RawLandingLayer(Path(self._tmp.name), "tuva")

    def _publish_snapshot(self, snapshot_id="snap-1", manifest=None):
        manifest = manifest or _manifest(snapshot_id)
        staging = self.layer.begin_staging(snapshot_id)
        (staging / "patient.csv").write_text("id\n1\n", encoding="utf-8")
        checksums = {"patient": {"sha256": "a" * 64, "size_bytes": 5}}
        return self.layer.finalize(staging, snapshot_id, manifest, checksums)

    def test_atomic_publication_and_success_marker(self):
        published = self._publish_snapshot()
        self.assertTrue((published.path / "_SUCCESS").is_file())
        self.assertTrue((published.path / "manifest.json").is_file())
        self.assertTrue((published.path / "checksums.json").is_file())
        self.assertTrue((published.path / "patient.csv").is_file())
        self.assertTrue(self.layer.is_published("snap-1"))

    def test_current_pointer_advances_only_after_success(self):
        self.assertIsNone(self.layer.current_snapshot_id())
        self._publish_snapshot("snap-1")
        self.assertEqual(self.layer.current_snapshot_id(), "snap-1")
        self._publish_snapshot("snap-2")
        self.assertEqual(self.layer.current_snapshot_id(), "snap-2")

    def test_current_pointer_not_advanced_on_aborted_staging(self):
        self._publish_snapshot("snap-1")
        staging = self.layer.begin_staging("snap-2")
        (staging / "patient.csv").write_text("bad", encoding="utf-8")
        self.layer.abort_staging(staging)
        self.assertEqual(self.layer.current_snapshot_id(), "snap-1")
        self.assertFalse(self.layer.is_published("snap-2"))
        self.assertFalse(staging.exists())

    def test_identical_snapshot_is_idempotent(self):
        manifest = _manifest("snap-1")
        self._publish_snapshot("snap-1", manifest)
        # Re-fetching the exact same manifest content should be a safe no-op.
        skip = self.layer.check_idempotent_or_conflicting("snap-1", manifest)
        self.assertTrue(skip)

    def test_conflicting_snapshot_content_fails_loudly(self):
        self._publish_snapshot("snap-1", _manifest("snap-1"))
        different = _manifest("snap-1")
        different["created_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(LandingError):
            self.layer.check_idempotent_or_conflicting("snap-1", different)

    def test_finalize_refuses_to_overwrite_completed_snapshot(self):
        self._publish_snapshot("snap-1")
        staging = self.layer.begin_staging("snap-1")
        with self.assertRaises(LandingError):
            self.layer.finalize(staging, "snap-1", _manifest("snap-1"), {})

    def test_restrictive_permissions(self):
        published = self._publish_snapshot()
        mode = stat.S_IMODE(published.path.stat().st_mode)
        self.assertEqual(mode, 0o750)
        file_mode = stat.S_IMODE((published.path / "patient.csv").stat().st_mode)
        self.assertEqual(file_mode, 0o640)

    def test_no_staging_leftover_after_publish(self):
        self._publish_snapshot()
        staging_root = self.layer._staging_root()
        self.assertEqual(list(staging_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
