"""Standard-library unit tests for tuva_ingest.manifest.

Run directly: python3 -m unittest tests.unit.test_manifest
or via `make test-unit` (python3 -m unittest discover -s tests/unit).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import ManifestError  # noqa: E402
from tuva_ingest.manifest import RAW_TABLES, parse_and_validate  # noqa: E402


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
            for t in RAW_TABLES
        ],
    }


class TestRawTablesMatchesInputLayer(unittest.TestCase):
    """RAW_TABLES is the authoritative claims Input Layer source set --
    exactly the three tables models/sources.yml declares and
    models/final/*.sql produces, so the manifest contract can never
    silently drift from what the dbt project actually maps."""

    def test_raw_tables_are_the_three_claims_feeds(self):
        self.assertEqual(RAW_TABLES, ("eligibility", "medical_claim", "pharmacy_claim"))

    def test_sources_yml_declares_exactly_raw_tables(self):
        sources_text = (REPO_ROOT / "models" / "sources.yml").read_text(encoding="utf-8")
        for table in RAW_TABLES:
            self.assertIn(f"name: {table}", sources_text)

    def test_final_model_exists_for_every_raw_table(self):
        for table in RAW_TABLES:
            self.assertTrue(
                (REPO_ROOT / "models" / "final" / f"{table}.sql").is_file(),
                f"missing models/final/{table}.sql for raw table {table!r}",
            )


class TestManifestValidation(unittest.TestCase):
    def test_valid_manifest_parses(self):
        manifest = parse_and_validate(_valid_manifest(), allow_insecure_http=False)
        self.assertEqual(manifest.version, 1)
        self.assertEqual(manifest.snapshot_id, "2026-08-14T060000Z")
        self.assertEqual(len(manifest.artifacts), len(RAW_TABLES))

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

    def test_artifact_for_looks_up_by_table(self):
        manifest = parse_and_validate(_valid_manifest(), allow_insecure_http=False)
        artifact = manifest.artifact_for("eligibility")
        self.assertEqual(artifact.table, "eligibility")
        self.assertEqual(artifact.filename, "eligibility.csv")




def _valid_endpoint_manifest(table: str) -> dict:
    return {
        "version": 1,
        "source": "tuva",
        "snapshot_id": "2026-08-14T060000Z",
        "created_at": "2026-08-14T06:00:00Z",
        "artifacts": [
            {
                "table": table,
                "url": f"https://example.invalid/snapshots/2026-08-14T060000Z/{table}.csv",
                "sha256": "a" * 64,
                "size_bytes": 1234,
            }
        ],
    }


class TestManifestEndpointScoping(unittest.TestCase):
    """extract --endpoint <name> requests and validates a manifest scoped
    to exactly one table via the new expected_tables parameter, while the
    legacy full-manifest flow (expected_tables=None) keeps validating
    against all of RAW_TABLES unchanged."""

    def test_expected_tables_none_still_requires_all_raw_tables(self):
        raw = _valid_endpoint_manifest("eligibility")
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False)

    def test_scoped_manifest_with_matching_single_artifact_is_valid(self):
        raw = _valid_endpoint_manifest("eligibility")
        manifest = parse_and_validate(raw, allow_insecure_http=False, expected_tables=("eligibility",))
        self.assertEqual(len(manifest.artifacts), 1)
        self.assertEqual(manifest.artifacts[0].table, "eligibility")

    def test_scoped_manifest_rejects_artifact_outside_requested_table(self):
        raw = _valid_endpoint_manifest("medical_claim")
        with self.assertRaises(ManifestError) as ctx:
            parse_and_validate(raw, allow_insecure_http=False, expected_tables=("eligibility",))
        self.assertIn("was not requested for this extraction", str(ctx.exception))

    def test_scoped_manifest_rejects_missing_requested_artifact(self):
        raw = _valid_endpoint_manifest("eligibility")
        raw["artifacts"] = []
        with self.assertRaises(ManifestError):
            parse_and_validate(raw, allow_insecure_http=False, expected_tables=("eligibility",))

    def test_scoped_manifest_accepts_each_supported_table_independently(self):
        for table in RAW_TABLES:
            with self.subTest(table=table):
                raw = _valid_endpoint_manifest(table)
                manifest = parse_and_validate(raw, allow_insecure_http=False, expected_tables=(table,))
                self.assertEqual([a.table for a in manifest.artifacts], [table])


if __name__ == "__main__":
    unittest.main()
