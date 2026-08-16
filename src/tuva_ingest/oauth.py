"""A dedicated OAuth 2.0 access-token manager for the ingestion
connector's source API, kept entirely separate from `api_client.py` so
token-lifecycle logic (acquisition, proactive refresh, rotation,
validation) lives in exactly one place rather than being embedded
throughout the HTTP client.

Grant assumption
-----------------
**Repository-derived assumption**: no OAuth grant is documented anywhere
in this repository (`docs/SOURCE_CONTRACT.md`/`docs/API_MANIFEST.md`
describe only a static bearer token). No specific vendor is integrated,
so this module implements the **client-credentials grant**
(`grant_type=client_credentials`) -- the standard, machine-to-machine
OAuth 2.0 grant (RFC 6749 SS4.4) -- as the initial/fallback grant, and
additionally supports `grant_type=refresh_token` when the token endpoint
has issued a `refresh_token` (RFC 6749 SS6), rotating to whatever new
refresh token (if any) the server returns on each refresh. If this
connector is later pointed at a real, documented vendor with a different
grant, update this assumption and this module together.

Lifecycle
----------
`OAuthTokenManager.get_access_token()` is the one method callers
(`api_client.ApiClient`) use: it returns a currently valid access token,
acquiring one on first use and proactively refreshing when the
remaining lifetime is at or below `refresh_skew_seconds` -- using a
refresh token if one is held, falling back to a fresh client-credentials
grant otherwise. Expiration is tracked as a **monotonic** deadline
(`clock() + expires_in` at acquisition time, using an injectable
monotonic clock -- never wall-clock time, which can jump) so a system
clock adjustment can never make this manager either serve an
already-expired token or refresh needlessly early.

`force_refresh()` unconditionally acquires a new token (used by
`ApiClient`'s single-shot 401-recovery path -- see its module docstring)
regardless of the current token's remaining lifetime.

Concurrency: a single `threading.Lock` serializes refreshes -- if two
threads both observe an expiring/expired token at once, only one of them
actually calls the token endpoint; the other blocks briefly and then
reads the token the first thread just acquired (double-checked locking:
the expiry check is repeated *inside* the lock before deciding to make a
real request).

Redaction: the access token, refresh token, and client secret are never
stored as plain `str` -- each is wrapped in `pydantic.SecretStr` so an
accidental `repr()`/`str()`/log call can never leak it, and
`OAuthTokenManager.__repr__`/`__str__` are overridden to the same
redacted shape `config.IngestConfig` already uses. `get_access_token()`
is the *only* way to read the real value out of this class; the full
token-endpoint response body is never retained or exposed after the
fields this module needs have been extracted from it.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from pydantic import SecretStr

from .errors import OAuthError
from .logging_utils import log_event
from .retry import RETRYABLE_STATUS, BoundedRetryExecutor, RetryBudgetExhausted

# RFC 6749 SS5.2 "error" values that indicate a *permanent* failure of
# this client's own credentials/request -- never retried, regardless of
# the bounded retry budget (retrying an invalid client secret only wastes
# the retry budget on a request that can never succeed).
_PERMANENT_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_client",
        "invalid_grant",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)

_RETRYABLE_HTTPX_EXCEPTIONS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.NetworkError)


@dataclass(frozen=True)
class _Token:
    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_at_monotonic: float
    token_type: str


def _redact_form(form: dict[str, str]) -> dict[str, str]:
    """Return a copy of an OAuth form body with every secret-shaped field
    redacted -- used only for structured log events / exception messages,
    never for the real outgoing request."""
    redacted = dict(form)
    for key in ("client_secret", "refresh_token", "code", "password"):
        if key in redacted:
            redacted[key] = "***REDACTED***"
    return redacted


class OAuthTokenManager:
    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: str | None = None,
        refresh_skew_seconds: float = 60.0,
        timeout: "httpx.Timeout | None" = None,
        max_retries: int = 5,
        max_retry_duration_seconds: float = 60.0,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        random_fn: Callable[[], float] = __import__("random").random,
        sleep_fn: Callable[[float], None] = time.sleep,
        logger: Any = None,
        run_id: str | None = None,
        transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = SecretStr(client_secret)
        self._scopes = scopes
        self._refresh_skew_seconds = refresh_skew_seconds
        self._clock = clock
        self._logger = logger
        self._run_id = run_id
        self._lock = threading.Lock()
        self._token: _Token | None = None

        resolved_timeout = timeout if timeout is not None else httpx.Timeout(30.0)
        self._client = httpx.Client(timeout=resolved_timeout, follow_redirects=False, transport=transport)
        self._executor = BoundedRetryExecutor(
            max_attempts=max_retries + 1,
            max_retry_duration_seconds=max_retry_duration_seconds,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            clock=clock,
            random_fn=random_fn,
            sleep_fn=sleep_fn,
            on_retry=self._log_retry,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OAuthTokenManager":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- redaction -------------------------------------------------------

    def __repr__(self) -> str:
        has_token = self._token is not None
        return f"OAuthTokenManager(token_url={self._token_url!r}, has_token={has_token})"

    def __str__(self) -> str:
        return self.__repr__()

    def _log_retry(self, *, attempt_number: int, delay_seconds: float, reason: str, status_code, elapsed_seconds: float) -> None:
        if self._logger is not None:
            log_event(
                self._logger, "http_retry_scheduled", run_id=self._run_id, endpoint="oauth_token",
                attempt=attempt_number, delay_seconds=round(delay_seconds, 3), status_code=status_code,
                reason=reason, elapsed_seconds=round(elapsed_seconds, 3),
            )

    # --- public accessor ---------------------------------------------------

    def get_access_token(self) -> str:
        """Return a currently valid access token, acquiring or proactively
        refreshing one as needed. Never returns an already-expired token.
        This is the only method that exposes the real token value."""
        token = self._token
        if token is not None and self._remaining_lifetime(token) > self._refresh_skew_seconds:
            return token.access_token.get_secret_value()

        with self._lock:
            # Re-check inside the lock: another thread may have already
            # refreshed while this one was waiting for the lock.
            token = self._token
            if token is not None and self._remaining_lifetime(token) > self._refresh_skew_seconds:
                return token.access_token.get_secret_value()
            return self._refresh_locked().access_token.get_secret_value()

    def force_refresh(self) -> str:
        """Unconditionally acquire a fresh token, regardless of the
        current token's remaining lifetime -- used for the single-shot
        401-recovery replay in `api_client.ApiClient`."""
        with self._lock:
            return self._refresh_locked().access_token.get_secret_value()

    # --- internal ------------------------------------------------------

    def _remaining_lifetime(self, token: _Token) -> float:
        return token.expires_at_monotonic - self._clock()

    def _refresh_locked(self) -> _Token:
        """Must be called with `self._lock` held. Uses the held refresh
        token (if any) via `grant_type=refresh_token`; falls back to a
        fresh `grant_type=client_credentials` request if no refresh token
        is available or the refresh attempt fails with a permanent OAuth
        error (an expired/revoked refresh token should not permanently
        wedge this manager -- one client-credentials fallback is
        attempted before giving up)."""
        current = self._token
        if current is not None and current.refresh_token is not None:
            try:
                token = self._request_token(
                    grant_type="refresh_token", refresh_token=current.refresh_token.get_secret_value()
                )
                self._token = token
                log_event(self._logger, "oauth_token_refreshed", run_id=self._run_id, grant_type="refresh_token")
                return token
            except OAuthError as exc:
                log_event(
                    self._logger, "oauth_token_refresh_failed", run_id=self._run_id, level=30,
                    error_category=exc.category, error_message=str(exc),
                )
                # Fall through to a fresh client-credentials grant only if
                # this failure could plausibly be an expired/revoked
                # refresh token, not a fully permanent client failure --
                # either way, one more attempt via client_credentials is
                # a bounded, safe fallback (never an unbounded retry loop).

        log_event(self._logger, "oauth_token_requested", run_id=self._run_id, grant_type="client_credentials")
        token = self._request_token(grant_type="client_credentials")
        self._token = token
        return token

    def _request_token(self, *, grant_type: str, refresh_token: str | None = None) -> _Token:
        form: dict[str, str] = {
            "grant_type": grant_type,
            "client_id": self._client_id,
            "client_secret": self._client_secret.get_secret_value(),
        }
        if grant_type == "refresh_token":
            form["refresh_token"] = refresh_token or ""
        if self._scopes:
            form["scope"] = self._scopes

        def _attempt() -> httpx.Response:
            request = self._client.build_request(
                "POST", self._token_url, data=form, headers={"Accept": "application/json"}
            )
            return self._client.send(request, stream=True)

        def _is_retryable_response(response: httpx.Response) -> bool:
            return response.status_code in RETRYABLE_STATUS

        def _get_retry_after(response: httpx.Response) -> str | None:
            return response.headers.get("Retry-After")

        def _close(response: httpx.Response) -> None:
            response.close()

        try:
            response = self._executor.run(
                _attempt,
                retryable_exceptions=_RETRYABLE_HTTPX_EXCEPTIONS,
                is_retryable_response=_is_retryable_response,
                get_retry_after=_get_retry_after,
                close_response=_close,
            )
        except RetryBudgetExhausted as exc:
            raise OAuthError(
                f"OAuth token request ({grant_type}) exhausted its retry budget (last: HTTP {exc.status_code})"
            ) from None
        except _RETRYABLE_HTTPX_EXCEPTIONS as exc:
            raise OAuthError(f"OAuth token request ({grant_type}) failed after retries: {exc.__class__.__name__}") from None

        try:
            return self._parse_token_response(response, grant_type=grant_type)
        finally:
            response.close()

    def _parse_token_response(self, response: httpx.Response, *, grant_type: str) -> _Token:
        if response.status_code != 200:
            body_snippet = self._safe_error_snippet(response)
            raise OAuthError(
                f"OAuth token request ({grant_type}) failed (HTTP {response.status_code}){body_snippet}"
            )

        content_type = response.headers.get("content-type", "")
        media_type = content_type.split(";")[0].strip().lower()
        if media_type and media_type != "application/json":
            raise OAuthError(
                f"OAuth token response has unsupported content type {content_type!r} (expected application/json)"
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise OAuthError(f"OAuth token response is not valid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise OAuthError(f"OAuth token response must be a JSON object, got {type(payload).__name__}")

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError("OAuth token response is missing a non-empty 'access_token' field")

        token_type = payload.get("token_type")
        if not isinstance(token_type, str) or token_type.strip().lower() != "bearer":
            raise OAuthError(f"OAuth token response has unsupported token_type {token_type!r} (expected 'Bearer')")

        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise OAuthError(f"OAuth token response has missing/invalid 'expires_in': {expires_in!r}")

        refresh_token_value = payload.get("refresh_token")
        if refresh_token_value is not None and not isinstance(refresh_token_value, str):
            raise OAuthError("OAuth token response 'refresh_token' must be a string if present")
        # Server may omit refresh_token on a refresh response, meaning
        # "keep using the one you already have" -- only replace the held
        # refresh token when the server actually sent a new one.
        if refresh_token_value:
            refresh_token = SecretStr(refresh_token_value)
        elif grant_type == "refresh_token" and self._token is not None:
            refresh_token = self._token.refresh_token
        else:
            refresh_token = None

        return _Token(
            access_token=SecretStr(access_token),
            refresh_token=refresh_token,
            expires_at_monotonic=self._clock() + float(expires_in),
            token_type=token_type,
        )

    @staticmethod
    def _safe_error_snippet(response: httpx.Response) -> str:
        """Best-effort, sanitized identification of a permanent OAuth
        error (invalid_client/invalid_grant/...) from the token
        endpoint's error body -- never includes the raw response body in
        the resulting exception message (it may contain
        request-echoing/diagnostic content this connector doesn't
        control)."""
        try:
            payload = response.json()
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        error_code = payload.get("error")
        if isinstance(error_code, str) and error_code in _PERMANENT_OAUTH_ERROR_CODES:
            return f": {error_code} (permanent -- not retried)"
        if isinstance(error_code, str):
            return f": {error_code}"
        return ""
