"""Standard-library unit tests for tuva_ingest.api_client.ApiClient.

Zero real network access: every test drives a scripted httpx.MockTransport
(see `_StepTransport` below) instead of a live HTTP server, per this
repository's testing policy (see the module docstring of api_client.py
and README.md's "Testing" section). `sleep_fn` is always injected as a
no-op recorder so retry/backoff tests run instantly and deterministically
-- no test in this file sleeps for real.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.api_client import ApiClient, user_agent  # noqa: E402
from tuva_ingest.errors import ChecksumError, DownloadError, ManifestError  # noqa: E402
from tuva_ingest.manifest import Artifact  # noqa: E402


class _StepTransport:
    """A scripted httpx transport: each call to `_handle` pops the next
    step off `steps` and either returns it (an httpx.Response) or raises
    it (an Exception) -- no real socket, DNS lookup, or server involved.
    `calls` records every httpx.Request actually sent, so tests can
    assert exact retry counts and header/query-param contents."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.steps:
            raise AssertionError("transport ran out of scripted responses/errors")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def as_httpx_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


def _client(steps, **kwargs) -> tuple[ApiClient, _StepTransport]:
    transport = _StepTransport(steps)
    sleeps: list[float] = kwargs.pop("_sleeps", None)
    if sleeps is None:
        sleeps = []
    kwargs.setdefault("sleep_fn", sleeps.append)
    kwargs.setdefault("max_retries", 3)
    client = ApiClient(token="test-token-xyz", transport=transport.as_httpx_transport(), **kwargs)
    return client, transport


def _manifest_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode("utf-8"))


class TestHeadersAndAuth(unittest.TestCase):
    def test_bearer_token_sent_on_every_request(self):
        client, transport = _client([_manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(transport.calls[0].headers["Authorization"], "Bearer test-token-xyz")

    def test_user_agent_header_matches_convention(self):
        client, transport = _client([_manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(transport.calls[0].headers["User-Agent"], user_agent())
        self.assertTrue(user_agent().startswith("tuva-ingest/"))

    def test_token_never_appears_in_a_raised_exception_message(self):
        client, transport = _client([httpx.Response(401)])
        with self.assertRaises(DownloadError) as ctx:
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertNotIn("test-token-xyz", str(ctx.exception))

    def test_token_never_appears_in_exhausted_retry_exception_message(self):
        client, transport = _client(
            [httpx.ConnectError("boom"), httpx.ConnectError("boom"), httpx.ConnectError("boom"), httpx.ConnectError("boom")],
            max_retries=3,
        )
        with self.assertRaises(DownloadError) as ctx:
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertNotIn("test-token-xyz", str(ctx.exception))


class TestQueryParamEncoding(unittest.TestCase):
    def test_params_passed_as_real_query_params_not_concatenated(self):
        client, transport = _client([_manifest_response({"ok": True})])
        client.fetch_manifest_json(
            "https://example.invalid/manifest.json", params={"endpoint": "medical-claims", "since": "2025-01-01"}
        )
        request = transport.calls[0]
        self.assertEqual(request.url.params["endpoint"], "medical-claims")
        self.assertEqual(request.url.params["since"], "2025-01-01")

    def test_params_with_special_characters_are_percent_encoded(self):
        client, transport = _client([_manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json", params={"since": "2025 01 01&x=1"})
        request = transport.calls[0]
        # httpx's own query encoding round-trips this back to the exact
        # original value -- proving it was never string-concatenated
        # into the URL path (which would corrupt/inject into it).
        self.assertEqual(request.url.params["since"], "2025 01 01&x=1")

    def test_no_params_means_no_query_string(self):
        client, transport = _client([_manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(str(transport.calls[0].url.params), "")


class TestFetchManifestJson(unittest.TestCase):
    def test_successful_fetch_parses_json_body(self):
        client, transport = _client([_manifest_response({"version": 1, "snapshot_id": "snap-1"})])
        result = client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(result, {"version": 1, "snapshot_id": "snap-1"})

    def test_401_raises_without_retry(self):
        client, transport = _client([httpx.Response(401)])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_403_raises_without_retry(self):
        client, transport = _client([httpx.Response(403)])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_404_raises_without_retry(self):
        client, transport = _client([httpx.Response(404)])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_400_raises_without_retry(self):
        client, transport = _client([httpx.Response(400)])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_invalid_json_raises_manifest_error(self):
        client, transport = _client([httpx.Response(200, content=b"not-json{{{")])
        with self.assertRaises(ManifestError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")

    def test_oversized_manifest_raises_manifest_error(self):
        from tuva_ingest.api_client import MAX_MANIFEST_BYTES

        oversized = b"x" * (MAX_MANIFEST_BYTES + 1)
        client, transport = _client([httpx.Response(200, content=oversized)])
        with self.assertRaises(ManifestError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")


class TestRetryBehavior(unittest.TestCase):
    def test_connect_error_then_success(self):
        client, transport = _client([httpx.ConnectError("boom"), _manifest_response({"ok": True})])
        result = client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(transport.calls), 2)

    def test_read_timeout_then_success(self):
        client, transport = _client([httpx.ReadTimeout("boom"), _manifest_response({"ok": True})])
        result = client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(transport.calls), 2)

    def test_429_then_success(self):
        client, transport = _client([httpx.Response(429), _manifest_response({"ok": True})])
        result = client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(transport.calls), 2)

    def test_500_then_success(self):
        client, transport = _client([httpx.Response(500), _manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 2)

    def test_502_then_success(self):
        client, transport = _client([httpx.Response(502), _manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 2)

    def test_503_then_success(self):
        client, transport = _client([httpx.Response(503), _manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 2)

    def test_504_then_success(self):
        client, transport = _client([httpx.Response(504), _manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 2)

    def test_retry_exhaustion_stops_at_exact_configured_count(self):
        # max_retries=2 -> stop_after_attempt(3) -> exactly 3 attempts,
        # never a 4th.
        steps = [httpx.ConnectError("boom")] * 3
        client, transport = _client(steps, max_retries=2)
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 3)

    def test_401_is_never_retried(self):
        client, transport = _client([httpx.Response(401), _manifest_response({"ok": True})])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_403_is_never_retried(self):
        client, transport = _client([httpx.Response(403), _manifest_response({"ok": True})])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_404_is_never_retried(self):
        client, transport = _client([httpx.Response(404), _manifest_response({"ok": True})])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_400_is_never_retried(self):
        client, transport = _client([httpx.Response(400), _manifest_response({"ok": True})])
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(transport.calls), 1)

    def test_retry_after_honored_but_capped_at_configured_max_delay(self):
        sleeps: list[float] = []
        client, transport = _client(
            [httpx.Response(429, headers={"Retry-After": "9999"}), _manifest_response({"ok": True})],
            max_retry_delay_seconds=2.0,
            _sleeps=sleeps,
        )
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 2.0)

    def test_exponential_backoff_never_exceeds_configured_max_delay(self):
        sleeps: list[float] = []
        client, transport = _client(
            [httpx.ConnectError("boom"), httpx.ConnectError("boom"), _manifest_response({"ok": True})],
            max_retry_delay_seconds=1.0,
            max_retries=3,
            _sleeps=sleeps,
        )
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(sleeps), 2)
        for sleep_value in sleeps:
            self.assertLessEqual(sleep_value, 1.0)

    def test_non_numeric_retry_after_falls_back_to_exponential_backoff(self):
        sleeps: list[float] = []
        client, transport = _client(
            [httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}), _manifest_response({"ok": True})],
            max_retry_delay_seconds=5.0,
            _sleeps=sleeps,
        )
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 5.0)

    def test_response_closed_before_each_retry(self):
        # A 429/5xx response's body must be closed (never left open) as
        # soon as retryability is determined -- otherwise a connection
        # pool slot would leak on every retried attempt.
        closed_flags: list[bool] = []

        real_response = httpx.Response(429)
        original_close = real_response.close

        def _tracking_close():
            closed_flags.append(True)
            return original_close()

        real_response.close = _tracking_close  # type: ignore[method-assign]

        client, transport = _client([real_response, _manifest_response({"ok": True})])
        client.fetch_manifest_json("https://example.invalid/manifest.json")
        self.assertEqual(closed_flags, [True])


class TestRedirectsNeverFollowed(unittest.TestCase):
    def test_redirect_response_is_not_transparently_followed(self):
        client, transport = _client(
            [httpx.Response(302, headers={"Location": "https://attacker.invalid/steal"})]
        )
        with self.assertRaises(DownloadError):
            client.fetch_manifest_json("https://example.invalid/manifest.json")
        # Exactly one request -- the client never automatically issued a
        # second request to the redirect target.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(str(transport.calls[0].url), "https://example.invalid/manifest.json")


class TestDownloadArtifact(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest_dir = Path(self._tmp.name)

    def _artifact(self, content: bytes) -> Artifact:
        return Artifact(
            table="eligibility",
            url="https://example.invalid/snap-1/eligibility.csv",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def test_successful_download_writes_file_and_returns_result(self):
        content = b"patient_id\n1\n2\n"
        artifact = self._artifact(content)
        client, transport = _client([httpx.Response(200, content=content)])
        result = client.download_artifact(artifact, self.dest_dir)
        self.assertEqual(result.sha256, artifact.sha256)
        self.assertEqual(result.size_bytes, len(content))
        self.assertTrue((self.dest_dir / "eligibility.csv").is_file())
        self.assertFalse((self.dest_dir / "eligibility.csv.part").exists())

    def test_checksum_mismatch_raises_and_removes_part_file(self):
        content = b"patient_id\n1\n2\n"
        artifact = self._artifact(content)
        # Serve different bytes than the manifest declared a checksum for.
        client, transport = _client([httpx.Response(200, content=b"tampered-content")])
        with self.assertRaises(ChecksumError):
            client.download_artifact(artifact, self.dest_dir)
        self.assertFalse((self.dest_dir / "eligibility.csv").exists())
        self.assertFalse((self.dest_dir / "eligibility.csv.part").exists())

    def test_size_mismatch_raises_and_removes_part_file(self):
        content = b"patient_id\n1\n2\n"
        artifact = self._artifact(content)
        client, transport = _client([httpx.Response(200, content=content + b"extra-unexpected-bytes")])
        with self.assertRaises(DownloadError):
            client.download_artifact(artifact, self.dest_dir)
        self.assertFalse((self.dest_dir / "eligibility.csv").exists())
        self.assertFalse((self.dest_dir / "eligibility.csv.part").exists())

    def test_exceeding_configured_max_artifact_bytes_aborts_and_cleans_up(self):
        artifact = Artifact(
            table="eligibility", url="https://example.invalid/snap-1/eligibility.csv",
            sha256="a" * 64, size_bytes=10,
        )
        oversized_content = b"x" * 5000
        client, transport = _client([httpx.Response(200, content=oversized_content)], max_artifact_bytes=1024)
        with self.assertRaises(DownloadError):
            client.download_artifact(artifact, self.dest_dir)
        self.assertFalse((self.dest_dir / "eligibility.csv").exists())
        self.assertFalse((self.dest_dir / "eligibility.csv.part").exists())

    def test_401_raises_without_retry(self):
        artifact = self._artifact(b"x")
        client, transport = _client([httpx.Response(401)])
        with self.assertRaises(DownloadError):
            client.download_artifact(artifact, self.dest_dir)
        self.assertEqual(len(transport.calls), 1)

    def test_404_raises_without_retry(self):
        artifact = self._artifact(b"x")
        client, transport = _client([httpx.Response(404)])
        with self.assertRaises(DownloadError):
            client.download_artifact(artifact, self.dest_dir)
        self.assertEqual(len(transport.calls), 1)

    def test_transient_failure_then_success_downloads_correctly(self):
        content = b"patient_id\n1\n2\n3\n"
        artifact = self._artifact(content)
        client, transport = _client([httpx.Response(503), httpx.Response(200, content=content)])
        result = client.download_artifact(artifact, self.dest_dir)
        self.assertEqual(result.sha256, artifact.sha256)
        self.assertEqual(len(transport.calls), 2)

    def test_no_part_file_left_over_after_success(self):
        content = b"a,b\n1,2\n"
        artifact = self._artifact(content)
        client, transport = _client([httpx.Response(200, content=content)])
        client.download_artifact(artifact, self.dest_dir)
        leftover_parts = list(self.dest_dir.glob("*.part"))
        self.assertEqual(leftover_parts, [])


class TestClientLifecycle(unittest.TestCase):
    def test_context_manager_closes_underlying_httpx_client(self):
        client, transport = _client([_manifest_response({"ok": True})])
        with client as ctx_client:
            self.assertIs(ctx_client, client)
            self.assertFalse(client._client.is_closed)
        self.assertTrue(client._client.is_closed)

    def test_close_is_idempotent_and_closes_httpx_client(self):
        client, transport = _client([_manifest_response({"ok": True})])
        client.close()
        self.assertTrue(client._client.is_closed)


if __name__ == "__main__":
    unittest.main()
