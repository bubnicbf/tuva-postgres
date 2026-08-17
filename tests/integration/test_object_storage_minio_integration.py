"""Opt-in S3-compatible (MinIO) integration test for
object_storage.s3.S3Backend -- uploads, publishes, reads, verifies, and
replays a complete run through a REAL S3 API implementation (never the
in-memory/local fakes the rest of this test suite uses).

*** Requires a running, disposable MinIO instance. ***

Skips (with an explicit, printed reason -- never a silent pass) unless
ALL of the following are true: `boto3` is installed, and
OBJECT_STORAGE_TEST_ENDPOINT_URL/OBJECT_STORAGE_TEST_BUCKET/
OBJECT_STORAGE_TEST_ACCESS_KEY_ID/OBJECT_STORAGE_TEST_SECRET_ACCESS_KEY
are all set. This test's boto3 client is constructed directly (not via
IngestConfig/S3Backend's own ambient-credential path) ONLY to supply the
disposable local MinIO credentials as ordinary AWS_ACCESS_KEY_ID/
AWS_SECRET_ACCESS_KEY process environment variables for the duration of
the test -- S3Backend itself is never handed a credential directly (see
object_storage/s3.py's module docstring); this test merely arranges for
boto3's own ambient chain to find one, exactly as it would from a real
IAM role in production.

Start MinIO locally: `docker compose up -d minio && docker compose run
--rm minio-mc` (see compose.yml), then:

    OBJECT_STORAGE_TEST_ENDPOINT_URL=http://localhost:9000 \\
    OBJECT_STORAGE_TEST_BUCKET=tuva-raw-local \\
    OBJECT_STORAGE_TEST_ACCESS_KEY_ID=tuva-local-minio \\
    OBJECT_STORAGE_TEST_SECRET_ACCESS_KEY=local-only-example-minio-secret-change-me \\
      python3 -m unittest tests.integration.test_object_storage_minio_integration -v

A skip here is NEVER proof the S3 backend works -- see
tests/unit/test_object_storage_backends.py/test_object_storage_publish_verify.py
for the deterministic, always-run fake-backend proof of the SAME
publish/verify contract; this test only additionally proves the real
boto3/S3-API wiring itself.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import boto3  # noqa: F401

    HAVE_BOTO3 = True
except ImportError:
    HAVE_BOTO3 = False

_ENDPOINT_URL = os.environ.get("OBJECT_STORAGE_TEST_ENDPOINT_URL")
_BUCKET = os.environ.get("OBJECT_STORAGE_TEST_BUCKET")
_ACCESS_KEY = os.environ.get("OBJECT_STORAGE_TEST_ACCESS_KEY_ID")
_SECRET_KEY = os.environ.get("OBJECT_STORAGE_TEST_SECRET_ACCESS_KEY")

_SKIP_REASON = None
if not HAVE_BOTO3:
    _SKIP_REASON = "boto3 is not installed (run `uv sync --locked --extra aws`)"
elif not (_ENDPOINT_URL and _BUCKET and _ACCESS_KEY and _SECRET_KEY):
    _SKIP_REASON = (
        "OBJECT_STORAGE_TEST_ENDPOINT_URL/OBJECT_STORAGE_TEST_BUCKET/"
        "OBJECT_STORAGE_TEST_ACCESS_KEY_ID/OBJECT_STORAGE_TEST_SECRET_ACCESS_KEY are not all set -- "
        "point them at a disposable local MinIO instance (see compose.yml's `minio` service) to run "
        "this suite"
    )

if _SKIP_REASON:
    print(f"tests.integration.test_object_storage_minio_integration: SKIPPED ({_SKIP_REASON})", file=sys.stderr)
else:
    from tuva_ingest.object_storage.keys import build_run_key, new_run_id  # noqa: E402
    from tuva_ingest.object_storage.publish import RunPublisher  # noqa: E402
    from tuva_ingest.object_storage.s3 import S3Backend  # noqa: E402
    from tuva_ingest.object_storage.verify import load_and_verify_manifest  # noqa: E402
    from tuva_ingest.errors import ObjectStorageError  # noqa: E402


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON or "")
class TestS3BackendAgainstMinio(unittest.TestCase):
    def setUp(self):
        os.environ["AWS_ACCESS_KEY_ID"] = _ACCESS_KEY
        os.environ["AWS_SECRET_ACCESS_KEY"] = _SECRET_KEY
        self.backend = S3Backend(bucket=_BUCKET, region="us-east-1", endpoint_url=_ENDPOINT_URL)
        self.run_key = build_run_key(
            vendor="acme", endpoint="eligibility", load_date=date(2026, 8, 14), run_id=new_run_id(),
        )

    def test_full_publish_read_verify_replay_cycle(self):
        publisher = RunPublisher(self.backend, self.run_key)
        records = [{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}]
        page = publisher.publish_page(1, records, request_cursor=None, response_cursor=None, next_page_cursor=None)
        manifest = publisher.publish_manifest(
            vendor="acme", endpoint="eligibility", requested_cursor=None, candidate_cursor="2026-08-14",
            pages=[page], extraction_started_at="2026-08-14T00:00:00.000000Z",
        )
        publisher.publish_success(manifest)

        # Read back through a FRESH backend instance (a new S3 client),
        # simulating a replay from a different process.
        replay_backend = S3Backend(bucket=_BUCKET, region="us-east-1", endpoint_url=_ENDPOINT_URL)
        verified = load_and_verify_manifest(replay_backend, self.run_key)
        self.assertEqual(verified.manifest["total_record_count"], 1)

    def test_overwrite_with_different_content_refused(self):
        from tuva_ingest.errors import ImmutableObjectError

        publisher = RunPublisher(self.backend, self.run_key)
        publisher.publish_page(1, [{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}], request_cursor=None, response_cursor=None, next_page_cursor=None)
        with self.assertRaises(ImmutableObjectError):
            publisher.publish_page(1, [{"person_id": "p2", "updated_at": "2026-08-14T00:00:00Z"}], request_cursor=None, response_cursor=None, next_page_cursor=None)


if __name__ == "__main__":
    unittest.main()
