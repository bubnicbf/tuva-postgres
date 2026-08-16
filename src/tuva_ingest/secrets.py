"""API credential retrieval, behind a small, injectable `SecretProvider`
interface -- so unit tests always use a fake provider and never contact a
real cloud service (see `TUVA_API_SECRET_PROVIDER`, `TUVA_API_SECRET_ID`,
`AWS_REGION` in `config.py`/`scripts/setup_env.example`).

No cloud/deployment convention existed anywhere else in this repository
(no Terraform, Kubernetes, ECS, Cloud Run, or Azure resource references)
when this module was written, so **AWS Secrets Manager** was implemented
as the initial real-cloud provider, per this task's own fallback
instruction. It authenticates using boto3's default *ambient* credential
chain only (an IAM role, an assumed role, `AWS_PROFILE`, or
`~/.aws/credentials` in local dev) -- this module never reads, accepts,
or configures a static AWS access key/secret pair anywhere.

Provider selection (`TUVA_API_SECRET_PROVIDER`, default `"env"`):

  "env"  Reads the credential directly from `TUVA_API_TOKEN` (the
         pre-existing environment variable). This is the default so every
         existing local-development, CI, and test workflow that already
         sets `TUVA_API_TOKEN` keeps working completely unchanged -- it is
         also exactly the "fake provider" shape unit tests want, just
         promoted to a first-class, always-available provider rather than
         a test-only stand-in. Not a production recommendation by itself;
         see "aws" below for real secret-manager-backed retrieval.
  "aws"  Retrieves the credential from AWS Secrets Manager
         (`TUVA_API_SECRET_ID`, optionally `AWS_REGION`) via boto3, using
         ambient identity only. `boto3` is imported lazily inside
         `AwsSecretsManagerProvider` (not at module scope) so this module
         -- and everything that imports it -- stays importable in an
         environment where `boto3` is not installed (e.g. this
         repository's own network-restricted CI/dev sandbox), exactly the
         same lazy-import convention `db.py` already uses for `psycopg`.

Expected secret JSON shape (the `SecretString` body of the AWS secret, or
-- for the "env" provider -- the synthesized equivalent):

    {"api_token": "<the bearer token>"}

At minimum `api_token` (a non-empty string) is required; unknown extra
keys are ignored (forward-compatible with a source that eventually needs
additional credential fields -- validate any such field explicitly here,
the same way `api_token` is validated, when a real source needs one).

The secret is retrieved exactly **once per process/run** -- `cli.py`
calls `retrieve_api_credential()` a single time per `extract`/`sync`
invocation and threads the resulting `ApiCredential` through, never
re-fetching it per page. The credential is never written to disk, and
`ApiCredential.api_token` is a `pydantic.SecretStr` specifically so an
accidental `print(credential)`/log call can never leak it -- exactly the
same pattern `config.IngestConfig.api_token`/`pg_dsn` already use.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

from .errors import SecretError
from .logging_utils import log_event

SUPPORTED_SECRET_PROVIDERS: tuple[str, ...] = ("env", "aws")


@dataclass(frozen=True)
class ApiCredential:
    """The resolved API credential for one run. `api_token` is a
    `SecretStr` -- never a bare `str` -- so the dataclass's own
    auto-generated `repr()`/`str()` can never leak it (SecretStr always
    renders as `SecretStr('**********')`); use `.api_token_value` at the
    one point an actual HTTP request needs the real value."""

    api_token: SecretStr

    @property
    def api_token_value(self) -> str:
        return self.api_token.get_secret_value()


class SecretProvider(Protocol):
    """The interface every secret provider implements. `get_secret_json`
    returns the parsed JSON secret payload (a `dict`) for `secret_id` --
    never a raw string the caller has to parse itself, and never anything
    already-redacted (validation/redaction happens one layer up, in
    `retrieve_api_credential`, so every provider's error-handling stays
    uniform)."""

    def get_secret_json(self, secret_id: str) -> dict[str, Any]: ...


class EnvSecretProvider:
    """Reads the credential directly from an environment variable
    (`TUVA_API_TOKEN` by default). This is the default provider (see
    module docstring) -- it deliberately does not require
    `TUVA_API_SECRET_ID` to be set, since the "secret" it reads is named
    by the env var itself, not by a lookup id."""

    def __init__(self, env: dict[str, str] | None = None, *, env_var: str = "TUVA_API_TOKEN") -> None:
        import os

        self._env = env if env is not None else os.environ
        self._env_var = env_var

    def get_secret_json(self, secret_id: str) -> dict[str, Any]:
        value = self._env.get(self._env_var)
        if not value:
            raise SecretError(
                f"secret provider 'env' could not find a credential: {self._env_var} is not set"
            )
        return {"api_token": value}


class AwsSecretsManagerProvider:
    """Retrieves the credential from AWS Secrets Manager. Authenticates
    via boto3's default ambient credential chain only (IAM role, assumed
    role, `AWS_PROFILE`, or a local developer profile) -- never a static
    access key configured by this connector. `region` is optional; when
    omitted, boto3's own region resolution (env var, profile, instance
    metadata) applies."""

    def __init__(self, *, region: str | None = None) -> None:
        self._region = region
        self._client: Any = None

    def _client_or_create(self) -> Any:
        if self._client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:
                raise SecretError(
                    "secret provider 'aws' requires the 'boto3' package, which is not installed "
                    "(run `uv sync --locked`)"
                ) from exc
            self._client = boto3.session.Session(region_name=self._region).client("secretsmanager")
        return self._client

    def get_secret_json(self, secret_id: str) -> dict[str, Any]:
        client = self._client_or_create()
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:  # noqa: BLE001 - translate every boto3/botocore error uniformly
            raise SecretError(
                f"secret provider 'aws' failed to retrieve secret {secret_id!r}: "
                f"{exc.__class__.__name__}"
            ) from exc

        secret_string = response.get("SecretString")
        if secret_string is None:
            raise SecretError(
                f"secret {secret_id!r} in AWS Secrets Manager has no SecretString payload "
                "(binary secrets are not supported)"
            )
        try:
            payload = json.loads(secret_string)
        except json.JSONDecodeError as exc:
            raise SecretError(f"secret {secret_id!r} is not valid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise SecretError(f"secret {secret_id!r} must be a JSON object, got {type(payload).__name__}")
        return payload


def get_secret_provider(config: Any) -> SecretProvider:
    """Select the configured `SecretProvider` from `config.api_secret_provider`
    (already validated to be one of `SUPPORTED_SECRET_PROVIDERS` by
    `IngestConfig`)."""
    if config.api_secret_provider == "env":
        return EnvSecretProvider()
    if config.api_secret_provider == "aws":
        return AwsSecretsManagerProvider(region=config.aws_region)
    raise SecretError(  # pragma: no cover - IngestConfig already rejects this before construction
        f"unsupported TUVA_API_SECRET_PROVIDER {config.api_secret_provider!r} "
        f"(supported: {', '.join(SUPPORTED_SECRET_PROVIDERS)})"
    )


def retrieve_api_credential(
    config: Any, *, provider: SecretProvider | None = None, logger: Any = None, run_id: str | None = None
) -> ApiCredential:
    """Retrieve, validate, and wrap the API credential for one run.
    Called exactly once per `extract`/`sync` invocation (see cli.py) --
    never once per page. `provider=` is dependency-injected so tests
    always pass a fake provider and never reach a real cloud service; when
    omitted, the provider is selected from `config` via
    `get_secret_provider`.

    Validates: the provider must actually return a JSON object; it must
    contain a non-empty string `api_token` field. Raises `SecretError` on
    any failure -- a missing secret, malformed JSON, a missing/blank
    `api_token`, or a provider-level error -- and the message never
    includes the secret value itself (there is nothing to redact in a
    "not found"/"malformed" message; the one thing that would be secret,
    the token value, only ever exists after validation succeeds, and is
    never interpolated into any message here or afterward).
    """
    active_provider = provider if provider is not None else get_secret_provider(config)

    secret_id = config.api_secret_id if config.api_secret_provider == "aws" else config.api_secret_provider
    if config.api_secret_provider == "aws" and not config.api_secret_id:
        raise SecretError(
            "TUVA_API_SECRET_PROVIDER=aws requires TUVA_API_SECRET_ID to be set "
            "(the AWS Secrets Manager secret name or ARN to retrieve)"
        )

    payload = active_provider.get_secret_json(secret_id or "")

    if not isinstance(payload, dict):
        raise SecretError(f"secret payload must be a JSON object, got {type(payload).__name__}")

    api_token = payload.get("api_token")
    if not isinstance(api_token, str) or not api_token.strip():
        raise SecretError("secret payload is missing a non-empty 'api_token' field")

    if logger is not None:
        log_event(
            logger, "secret_retrieved", run_id=run_id,
            provider=config.api_secret_provider, secret_id=config.api_secret_id,
        )

    return ApiCredential(api_token=SecretStr(api_token))
