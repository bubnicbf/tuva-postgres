"""Standard-library unit tests for tuva_ingest.oauth.OAuthTokenManager.

Zero real network access: every test drives a scripted httpx.MockTransport
against the token endpoint (mirroring test_api_client.py's `_StepTransport`
pattern) with `sleep_fn`/`clock`/`random_fn` always injected so retry/
backoff paths never sleep for real and run instantly.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import OAuthError  # noqa: E402
from tuva_ingest.oauth import OAuthTokenManager  # noqa: E402


class _StepTransport:
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


def _token_response(access_token="tok-1", token_type="Bearer", expires_in=3600, refresh_token=None, status=200):
    body = {"access_token": access_token, "token_type": token_type, "expires_in": expires_in}
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    return httpx.Response(
        status, content=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json"}
    )


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _manager(steps, *, clock=None, **kwargs):
    transport = _StepTransport(steps)
    clock = clock or _FakeClock()
    sleeps = []
    manager = OAuthTokenManager(
        token_url="https://example.invalid/oauth/token",
        client_id="client-id-1",
        client_secret="super-secret-value-xyz",
        transport=transport.as_httpx_transport(),
        clock=clock,
        sleep_fn=lambda s: (sleeps.append(s), clock.advance(s)),
        random_fn=lambda: 0.0,
        max_retries=3,
        **kwargs,
    )
    return manager, transport, clock, sleeps


class TestInitialAcquisition(unittest.TestCase):
    def test_acquires_token_on_first_use(self):
        manager, transport, _, _ = _manager([_token_response(access_token="first-token")])
        token = manager.get_access_token()
        self.assertEqual(token, "first-token")
        self.assertEqual(len(transport.calls), 1)

    def test_uses_client_credentials_grant_by_default(self):
        manager, transport, _, _ = _manager([_token_response()])
        manager.get_access_token()
        body = transport.calls[0].content.decode("utf-8")
        self.assertIn("grant_type=client_credentials", body)
        self.assertIn("client_id=client-id-1", body)

    def test_scopes_included_when_configured(self):
        manager, transport, _, _ = _manager([_token_response()], scopes="read write")
        manager.get_access_token()
        body = transport.calls[0].content.decode("utf-8")
        self.assertIn("scope=", body)

    def test_token_not_re_acquired_when_still_valid(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager([_token_response(expires_in=3600)], clock=clock)
        manager.get_access_token()
        manager.get_access_token()
        self.assertEqual(len(transport.calls), 1)


class TestProactiveRefresh(unittest.TestCase):
    def test_refreshes_when_remaining_lifetime_at_or_below_skew(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [_token_response(access_token="tok-1", expires_in=100), _token_response(access_token="tok-2", expires_in=100)],
            clock=clock, refresh_skew_seconds=30,
        )
        self.assertEqual(manager.get_access_token(), "tok-1")
        clock.advance(71)  # remaining lifetime = 100 - 71 = 29 <= skew(30)
        self.assertEqual(manager.get_access_token(), "tok-2")
        self.assertEqual(len(transport.calls), 2)

    def test_no_refresh_while_safely_before_expiration(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [_token_response(access_token="tok-1", expires_in=100)], clock=clock, refresh_skew_seconds=30,
        )
        self.assertEqual(manager.get_access_token(), "tok-1")
        clock.advance(50)  # remaining lifetime = 50 > skew(30)
        self.assertEqual(manager.get_access_token(), "tok-1")
        self.assertEqual(len(transport.calls), 1)

    def test_never_returns_an_expired_token(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [_token_response(access_token="tok-1", expires_in=10), _token_response(access_token="tok-2", expires_in=10)],
            clock=clock, refresh_skew_seconds=1,
        )
        manager.get_access_token()
        clock.advance(15)  # fully expired
        token = manager.get_access_token()
        self.assertEqual(token, "tok-2")


class TestRefreshTokenRotation(unittest.TestCase):
    def test_uses_refresh_token_grant_when_available(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [
                _token_response(access_token="tok-1", expires_in=10, refresh_token="refresh-1"),
                _token_response(access_token="tok-2", expires_in=10, refresh_token="refresh-2"),
            ],
            clock=clock, refresh_skew_seconds=1,
        )
        manager.get_access_token()
        clock.advance(15)
        manager.get_access_token()
        second_body = transport.calls[1].content.decode("utf-8")
        self.assertIn("grant_type=refresh_token", second_body)
        self.assertIn("refresh_token=refresh-1", second_body)

    def test_rotates_to_the_latest_refresh_token(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [
                _token_response(access_token="tok-1", expires_in=10, refresh_token="refresh-1"),
                _token_response(access_token="tok-2", expires_in=10, refresh_token="refresh-2"),
                _token_response(access_token="tok-3", expires_in=10, refresh_token="refresh-3"),
            ],
            clock=clock, refresh_skew_seconds=1,
        )
        manager.get_access_token()
        clock.advance(15)
        manager.get_access_token()
        clock.advance(15)
        manager.get_access_token()
        third_body = transport.calls[2].content.decode("utf-8")
        self.assertIn("refresh_token=refresh-2", third_body)

    def test_server_omitting_refresh_token_on_refresh_keeps_the_held_one(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [
                _token_response(access_token="tok-1", expires_in=10, refresh_token="refresh-1"),
                _token_response(access_token="tok-2", expires_in=10),  # no refresh_token this time
                _token_response(access_token="tok-3", expires_in=10),
            ],
            clock=clock, refresh_skew_seconds=1,
        )
        manager.get_access_token()
        clock.advance(15)
        manager.get_access_token()
        clock.advance(15)
        manager.get_access_token()
        third_body = transport.calls[2].content.decode("utf-8")
        self.assertIn("refresh_token=refresh-1", third_body)

    def test_client_credentials_fallback_when_no_refresh_token_available(self):
        clock = _FakeClock()
        manager, transport, _, _ = _manager(
            [
                _token_response(access_token="tok-1", expires_in=10),  # no refresh_token
                _token_response(access_token="tok-2", expires_in=10),
            ],
            clock=clock, refresh_skew_seconds=1,
        )
        manager.get_access_token()
        clock.advance(15)
        manager.get_access_token()
        second_body = transport.calls[1].content.decode("utf-8")
        self.assertIn("grant_type=client_credentials", second_body)


class TestInvalidTokenResponses(unittest.TestCase):
    def test_missing_access_token_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=json.dumps({"token_type": "Bearer", "expires_in": 10}).encode(), headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()

    def test_invalid_token_type_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=json.dumps({"access_token": "x", "token_type": "MAC", "expires_in": 10}).encode(), headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()

    def test_missing_expires_in_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=json.dumps({"access_token": "x", "token_type": "Bearer"}).encode(), headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()

    def test_non_positive_expires_in_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=json.dumps({"access_token": "x", "token_type": "Bearer", "expires_in": 0}).encode(), headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()

    def test_malformed_json_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=b"not-json{{{", headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()

    def test_unsupported_content_type_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=b"<html>nope</html>", headers={"content-type": "text/html"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()

    def test_non_object_json_raises(self):
        manager, _, _, _ = _manager([
            httpx.Response(200, content=b"[1,2,3]", headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()


class TestPermanentFailuresAreNeverRetried(unittest.TestCase):
    def test_invalid_client_400_is_not_retried(self):
        manager, transport, _, sleeps = _manager([
            httpx.Response(400, content=json.dumps({"error": "invalid_client"}).encode(), headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleeps, [])

    def test_invalid_grant_400_is_not_retried(self):
        manager, transport, _, sleeps = _manager([
            httpx.Response(400, content=json.dumps({"error": "invalid_grant"}).encode(), headers={"content-type": "application/json"})
        ])
        with self.assertRaises(OAuthError):
            manager.get_access_token()
        self.assertEqual(len(transport.calls), 1)


class TestTransientFailuresAreRetried(unittest.TestCase):
    def test_503_then_success_is_retried(self):
        manager, transport, _, sleeps = _manager([
            httpx.Response(503, content=b"", headers={"content-type": "application/json"}),
            _token_response(access_token="tok-after-retry"),
        ])
        token = manager.get_access_token()
        self.assertEqual(token, "tok-after-retry")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(sleeps), 1)

    def test_connection_error_then_success_is_retried(self):
        manager, transport, _, sleeps = _manager([
            httpx.ConnectError("boom"),
            _token_response(access_token="tok-after-retry"),
        ])
        token = manager.get_access_token()
        self.assertEqual(token, "tok-after-retry")
        self.assertEqual(len(sleeps), 1)


class TestRedaction(unittest.TestCase):
    def test_repr_never_includes_client_secret_or_token(self):
        manager, _, _, _ = _manager([_token_response(access_token="ultra-secret-token-value")])
        manager.get_access_token()
        text = repr(manager)
        self.assertNotIn("ultra-secret-token-value", text)
        self.assertNotIn("super-secret-value-xyz", text)

    def test_str_never_includes_client_secret_or_token(self):
        manager, _, _, _ = _manager([_token_response(access_token="ultra-secret-token-value")])
        manager.get_access_token()
        self.assertNotIn("ultra-secret-token-value", str(manager))

    def test_client_secret_never_sent_in_the_clear_as_a_header(self):
        manager, transport, _, _ = _manager([_token_response()])
        manager.get_access_token()
        for header_value in transport.calls[0].headers.values():
            self.assertNotIn("super-secret-value-xyz", header_value)

    def test_oauth_error_message_never_includes_client_secret(self):
        manager, _, _, _ = _manager([
            httpx.Response(400, content=json.dumps({"error": "invalid_client"}).encode(), headers={"content-type": "application/json"})
        ])
        try:
            manager.get_access_token()
        except OAuthError as exc:
            self.assertNotIn("super-secret-value-xyz", str(exc))
        else:
            self.fail("expected OAuthError")


class TestForceRefresh(unittest.TestCase):
    def test_force_refresh_ignores_remaining_lifetime(self):
        manager, transport, _, _ = _manager([
            _token_response(access_token="tok-1", expires_in=3600),
            _token_response(access_token="tok-2", expires_in=3600),
        ])
        manager.get_access_token()
        token = manager.force_refresh()
        self.assertEqual(token, "tok-2")
        self.assertEqual(len(transport.calls), 2)


class TestConcurrency(unittest.TestCase):
    def test_concurrent_get_access_token_calls_do_not_duplicate_refresh(self):
        # A transport that blocks the *first* caller inside the token
        # request just long enough for a second thread's call to also
        # observe "needs refresh" -- proving the lock prevents two real
        # token requests from firing.
        release = threading.Event()
        first_call_started = threading.Event()
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                first_call_started.set()
                release.wait(timeout=5)
            return _token_response(access_token=f"tok-{call_count['n']}")

        manager = OAuthTokenManager(
            token_url="https://example.invalid/oauth/token",
            client_id="client-id-1",
            client_secret="secret",
            transport=httpx.MockTransport(handler),
            clock=time.monotonic,
            sleep_fn=lambda s: None,
            random_fn=lambda: 0.0,
        )

        results = []

        def worker():
            results.append(manager.get_access_token())

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        first_call_started.wait(timeout=5)
        t2.start()
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(call_count["n"], 1)  # only one real token request happened
        self.assertEqual(results, ["tok-1", "tok-1"])


if __name__ == "__main__":
    unittest.main()
