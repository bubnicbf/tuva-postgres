"""Standard-library unit tests for tuva_postgres.manifest.

Run directly: python3 -m unittest tests.unit.test_manifest
or via `make test-unit` (python3 -m unittest discover -s tests/unit).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.errors import ManifestError  # noqa: E402
from tuva_postgres.manifest import MANAGED_TABLES, parse_and_validate  # noqa: E402


def _valid_manifest() -> dict:
    return {
        "version": 1,
        "source": "tuva",
        "snapshot_id": "2026-08-14T060000Z",
        "created_at": "2026-08-14T06:00:00Z",
        "artifacts": [
            {
                "table": t,
                "url": f"https://example.invalid/snapshots/2026-08-14T060000Z/{t}.csv",
                "sha256": "a" * 64,
                "size_bytes": 1234,
            }
            for t in MANAGED_TABLES
        ],
    }


class TestManagedTablesSyncedWithLoader(unittest.TestCase):
    """MANAGED_TABLES must be the authoritative expected dataset -- proven
    by asserting it matches scripts/load_to_postgres.sh's bash array
    exactly, so the two can never silently drift apart."""

    def test_matches_loader_bash_array(self):
        loader_text = (REPO_ROOT / "scripts" / "load_to_postgres.sh").read_text(encoding="utf-8")
        m = re.search(r"declare -a MANAGED_TABLES=\((.*?)\)", loader_text, re.DOTALL)
        self.assertIsNotNone(m, "could not find MANAGED_TABLES array in load_to_postgres.sh")
        bash_tables = tuple(re.findall(r'"([a-z_]+)"', m.group(1)))
        self.assertEqual(bash_tables, MANAGED_TABLES)


class TestManifestValidation(unittest.TestCase):
    def test_valid_manifest_parses(self):
        manifest = parse_and_validate(_valid_manifest(), allow_insecure_http=False)
        self.assertEqual(manifest.version, 1)
        self.assertEqual(manifest.snapshot_id, "2026-08-14T060000Z")
        self.assertEqual(len(manifest.artifacts), len(MANAGED_TABLES))

    def test_unsupported_version_rejected(self):
        raw = _valid_manifest()
        raw["version"] = 99
        with self.assertRaises(ManifestError) as ctx:
            parse_and_validate(raw, allow_insecure_http=False)
        self.assertIn("version", str(ctx.exception))

    def test_empty_source_rejected(self):
        raw = _valid_manifest()
        raw["source"] = ""
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_unsafe_snapshot_id_rejected(self):
        raw = _valid_manifest()
        raw["snapshot_id"] = "../../etc/passwd"
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_invalid_timestamp_rejected(self):
        raw = _valid_manifest()
        raw["created_at"] = "not-a-date"
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_missing_artifact_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"].pop()
        with self.assertRaises(ManifestError) as ctx:
            parse_and_validate(raw, allow_insecure_http=False)
        self.assertIn("missing artifact", str(ctx.exception))

    def test_duplicate_table_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"].append(dict(raw["artifacts"][0]))
        with self.assertRaises(ManifestError) as ctx:
            parse_and_validate(raw, allow_insecure_http=False)
        self.assertIn("duplicate", str(ctx.exception))

    def test_unknown_table_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"][0] = {
            "table": "not_a_real_table",
            "url": "https://example.invalid/x.csv",
            "sha256": "a" * 64,
            "size_bytes": 1,
        }
        with self.assertRaises(ManifestError) as ctx:
            parse_and_validate(raw, allow_insecure_http=False)
        self.assertIn("unknown", str(ctx.exception).lower())

    def test_insecure_http_rejected_by_default(self):
        raw = _valid_manifest()
        raw["artifacts"][0]["url"] = raw["artifacts"][0]["url"].replace("https://", "http://")
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_insecure_http_allowed_when_enabled(self):
        raw = _valid_manifest()
        for artifact in raw["artifacts"]:
            artifact["url"] = artifact["url"].replace("https://", "http://")
        manifest = parse_and_validate(raw, allow_insecure_http=True)
        self.assertTrue(all(a.url.startswith("http://") for a in manifest.artifacts))

    def test_negative_size_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"][0]["size_bytes"] = -1
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_bad_sha256_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"][0]["sha256"] = "not-hex"
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_uppercase_sha256_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"][0]["sha256"] = "A" * 64
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_path_traversal_in_url_rejected(self):
        raw = _valid_manifest()
        raw["artifacts"][0]["url"] = "https://example.invalid/snapshots/../../etc/passwd"
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)


if __name__ == "__main__":
    unittest.main()
