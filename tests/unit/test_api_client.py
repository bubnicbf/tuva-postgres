"""Unit tests for tuva_postgres.api_client, against a real in-process mock
HTTP server (http.server), not a proprietary/external API.

Requires the `requests` runtime dependency (see pyproject.toml /
scripts/tests/test_python_dependencies.py-style lock verification) --
these tests are skipped with a clear reason if it isn't installed, rather
than failing the whole suite in an environment where deps haven't been
synced yet.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import requests as _requests  # noqa: F401

    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

if HAVE_REQUESTS:
    from tuva_postgres.api_client import ApiClient
    from tuva_postgres.errors import ChecksumError, DownloadError
    from tuva_postgres.manifest import Artifact

TOKEN = "s3cr3t-test-token-do-not-leak"
CONTENT = b"id,name\n1,alice\n2,bob\n"
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()


class _Server:
    """A minimal in-process HTTP server whose behavior for each request is
    decided by a caller-supplied handler function, so each test can script
    exactly the response sequence it needs (retries, auth failures, etc.)
    without any real network access."""

    def __init__(self, handle_fn):
        self.requests_seen: list[dict] = []
        handle_fn_ref = handle_fn
        requests_seen_ref = self.requests_seen

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence test output
                pass

            def do_GET(self):
                requests_seen_ref.append({"path": self.path, "headers": dict(self.headers)})
                handle_fn_ref(self)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(HAVE_REQUESTS, "requests is not installed in this environment")
class TestApiClient(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest_dir = Path(self._tmp.name)
        self._servers: list[_Server] = []

    def tearDown(self):
        for server in self._servers:
            server.stop()

    def _start(self, handle_fn) -> _Server:
        server = _Server(handle_fn)
        self._servers.append(server)
        return server

    def _artifact(self, url: str) -> Artifact:
        return Artifact(table="patient", url=url, sha256=CONTENT_SHA256, size_bytes=len(CONTENT))

    def _client(self, max_retries=2, sleep_fn=lambda s: None) -> ApiClient:
        return ApiClient(
            token=TOKEN, timeout_seconds=5, max_retries=max_retries, logger=None, sleep_fn=sleep_fn
        )

    # --- happy path -----------------------------------------------------
    def test_bearer_auth_header_sent(self):
        seen_auth = {}

        def handle(h):
            seen_auth["value"] = h.headers.get("Authorization")
            h.send_response(200)
            h.send_header("Content-Length", str(len(CONTENT)))
            h.end_headers()
            h.wfile.write(CONTENT)

        server = self._start(handle)
        client = self._client()
        result = client.download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertEqual(seen_auth["value"], f"Bearer {TOKEN}")
        self.assertEqual(result.sha256, CONTENT_SHA256)
        self.assertTrue((self.dest_dir / "patient.csv").is_file())
        self.assertFalse((self.dest_dir / "patient.csv.part").exists())

    def test_user_agent_sent(self):
        seen = {}

        def handle(h):
            seen["ua"] = h.headers.get("User-Agent")
            h.send_response(200)
            h.send_header("Content-Length", str(len(CONTENT)))
            h.end_headers()
            h.wfile.write(CONTENT)

        server = self._start(handle)
        self._client().download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertIn("tuva-postgres/", seen["ua"])

    # --- integrity checks -------------------------------------------------
    def test_checksum_mismatch_raises_and_cleans_up(self):
        def handle(h):
            h.send_response(200)
            wrong = b"not the right content!!"
            h.send_header("Content-Length", str(len(wrong)))
            h.end_headers()
            h.wfile.write(wrong)

        server = self._start(handle)
        with self.assertRaises((ChecksumError, DownloadError)):
            self._client().download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertFalse((self.dest_dir / "patient.csv").exists())
        self.assertFalse((self.dest_dir / "patient.csv.part").exists())

    def test_size_mismatch_raises_and_cleans_up(self):
        def handle(h):
            h.send_response(200)
            short = CONTENT[:5]
            h.send_header("Content-Length", str(len(short)))
            h.end_headers()
            h.wfile.write(short)

        server = self._start(handle)
        with self.assertRaises(DownloadError):
            self._client().download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertFalse((self.dest_dir / "patient.csv").exists())
        self.assertFalse((self.dest_dir / "patient.csv.part").exists())

    # --- retry behavior -----------------------------------------------------
    def test_429_is_retried_then_succeeds(self):
        attempts = {"n": 0}

        def handle(h):
            attempts["n"] += 1
            if attempts["n"] == 1:
                h.send_response(429)
                h.send_header("Retry-After", "0")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            h.send_response(200)
            h.send_header("Content-Length", str(len(CONTENT)))
            h.end_headers()
            h.wfile.write(CONTENT)

        server = self._start(handle)
        result = self._client().download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(result.sha256, CONTENT_SHA256)

    def test_503_is_retried_then_succeeds(self):
        attempts = {"n": 0}

        def handle(h):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                h.send_response(503)
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            h.send_response(200)
            h.send_header("Content-Length", str(len(CONTENT)))
            h.end_headers()
            h.wfile.write(CONTENT)

        server = self._start(handle)
        result = self._client(max_retries=3).download_artifact(
            self._artifact(server.base_url + "/patient.csv"), self.dest_dir
        )
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(result.sha256, CONTENT_SHA256)

    def test_404_is_not_retried(self):
        attempts = {"n": 0}

        def handle(h):
            attempts["n"] += 1
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()

        server = self._start(handle)
        with self.assertRaises(DownloadError):
            self._client(max_retries=3).download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertEqual(attempts["n"], 1)

    def test_401_is_not_retried_and_token_not_leaked(self):
        attempts = {"n": 0}

        def handle(h):
            attempts["n"] += 1
            h.send_response(401)
            h.send_header("Content-Length", "0")
            h.end_headers()

        server = self._start(handle)
        with self.assertRaises(DownloadError) as ctx:
            self._client(max_retries=3).download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)
        self.assertEqual(attempts["n"], 1)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_exhausted_retries_raises(self):
        def handle(h):
            h.send_response(500)
            h.send_header("Content-Length", "0")
            h.end_headers()

        server = self._start(handle)
        with self.assertRaises(DownloadError):
            self._client(max_retries=1).download_artifact(self._artifact(server.base_url + "/patient.csv"), self.dest_dir)

    def test_manifest_fetch_and_json_parsing(self):
        import json

        payload = {"version": 1, "source": "tuva"}
        body = json.dumps(payload).encode("utf-8")

        def handle(h):
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)

        server = self._start(handle)
        result = self._client().fetch_manifest_json(server.base_url + "/manifest.json")
        self.assertEqual(result, payload)


if __name__ == "__main__":
    unittest.main()
