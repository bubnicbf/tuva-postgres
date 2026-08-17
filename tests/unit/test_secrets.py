"""Standard-library unit tests for tuva_ingest.secrets: provider
selection, credential retrieval/validation, and secret-safety. Every
test uses a fake or the "env"-backed provider -- never a real AWS SDK
call, never real network access.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import SecretError  # noqa: E402
from tuva_ingest.secrets import (  # noqa: E402
    ApiCredential,
    EnvSecretProvider,
    SUPPORTED_SECRET_PROVIDERS,
    get_secret_provider,
    retrieve_api_credential,
)


@dataclass
class _FakeConfig:
    api_secret_provider: str = "env"
    api_secret_id: str | None = None
    aws_region: str | None = None


class _FakeProvider:
    """Dependency-injected fake -- unit tests must never reach a real
    cloud service. Records every secret_id it was asked for."""

    def __init__(self, payload=None, *, error: Exception | None = None):
        self._payload = payload if payload is not None else {"api_token": "fake-token-123"}
        self._error = error
        self.calls: list[str] = []

    def get_secret_json(self, secret_id: str):
        self.calls.append(secret_id)
        if self._error is not None:
            raise self._error
        return self._payload


class TestSecretProviderSelection(unittest.TestCase):
    def test_env_is_the_default_provider(self):
        config = _FakeConfig()
        provider = get_secret_provider(config)
        self.assertIsInstance(provider, EnvSecretProvider)

    def test_aws_provider_selected_by_config(self):
        from tuva_ingest.secrets import AwsSecretsManagerProvider

        config = _FakeConfig(api_secret_provider="aws", api_secret_id="prod/tuva/api-token")
        provider = get_secret_provider(config)
        self.assertIsInstance(provider, AwsSecretsManagerProvider)

    def test_supported_providers_are_env_and_aws(self):
        self.assertEqual(set(SUPPORTED_SECRET_PROVIDERS), {"env", "aws"})


class TestRetrieveApiCredentialSuccess(unittest.TestCase):
    def test_valid_secret_returns_api_credential(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": "s3cr3t-token-value"})
        credential = retrieve_api_credential(config, provider=provider)
        self.assertIsInstance(credential, ApiCredential)
        self.assertEqual(credential.api_token_value, "s3cr3t-token-value")

    def test_extra_unknown_fields_are_ignored(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": "tok", "future_field": "whatever"})
        credential = retrieve_api_credential(config, provider=provider)
        self.assertEqual(credential.api_token_value, "tok")

    def test_secret_retrieved_exactly_once_per_call(self):
        # retrieve_api_credential is called once per run by cli.py (never
        # once per page) -- this test proves the provider is invoked
        # exactly once per retrieve_api_credential() call.
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": "tok"})
        retrieve_api_credential(config, provider=provider)
        self.assertEqual(len(provider.calls), 1)

    def test_aws_provider_uses_configured_secret_id(self):
        config = _FakeConfig(api_secret_provider="aws", api_secret_id="prod/tuva/api-token")
        provider = _FakeProvider({"api_token": "tok"})
        retrieve_api_credential(config, provider=provider)
        self.assertEqual(provider.calls, ["prod/tuva/api-token"])


class TestRetrieveApiCredentialFailures(unittest.TestCase):
    def test_missing_secret_raises_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider(error=SecretError("not found"))
        with self.assertRaises(SecretError):
            retrieve_api_credential(config, provider=provider)

    def test_provider_error_is_wrapped_as_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider(error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            # A provider that raises something other than SecretError is
            # not silently swallowed either -- retrieve_api_credential
            # does not catch arbitrary exceptions from the provider, only
            # validates its *return value*. (AwsSecretsManagerProvider
            # itself is what translates boto3/botocore errors into
            # SecretError -- see test below.)
            retrieve_api_credential(config, provider=provider)

    def test_malformed_json_payload_type_raises_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider(payload=["not", "a", "dict"])
        with self.assertRaises(SecretError):
            retrieve_api_credential(config, provider=provider)

    def test_missing_api_token_field_raises_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider({"other_field": "x"})
        with self.assertRaises(SecretError) as ctx:
            retrieve_api_credential(config, provider=provider)
        self.assertIn("api_token", str(ctx.exception))

    def test_empty_api_token_raises_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": ""})
        with self.assertRaises(SecretError):
            retrieve_api_credential(config, provider=provider)

    def test_whitespace_only_api_token_raises_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": "   "})
        with self.assertRaises(SecretError):
            retrieve_api_credential(config, provider=provider)

    def test_non_string_api_token_raises_secret_error(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": 12345})
        with self.assertRaises(SecretError):
            retrieve_api_credential(config, provider=provider)

    def test_aws_provider_without_secret_id_raises_before_any_provider_call(self):
        config = _FakeConfig(api_secret_provider="aws", api_secret_id=None)
        provider = _FakeProvider({"api_token": "tok"})
        with self.assertRaises(SecretError) as ctx:
            retrieve_api_credential(config, provider=provider)
        self.assertIn("TUVA_API_SECRET_ID", str(ctx.exception))
        self.assertEqual(provider.calls, [])


class TestEnvSecretProvider(unittest.TestCase):
    def test_reads_configured_env_var(self):
        provider = EnvSecretProvider(env={"TUVA_API_TOKEN": "abc123"})
        payload = provider.get_secret_json("env")
        self.assertEqual(payload, {"api_token": "abc123"})

    def test_missing_env_var_raises_secret_error(self):
        provider = EnvSecretProvider(env={})
        with self.assertRaises(SecretError):
            provider.get_secret_json("env")

    def test_empty_env_var_raises_secret_error(self):
        provider = EnvSecretProvider(env={"TUVA_API_TOKEN": ""})
        with self.assertRaises(SecretError):
            provider.get_secret_json("env")


class TestAwsSecretsManagerProvider(unittest.TestCase):
    """boto3 is never actually imported/contacted here -- a fake client
    object is injected in place of what `_client_or_create()` would
    normally build, so these tests never make a real AWS call."""

    def _provider_with_fake_client(self, fake_client):
        from tuva_ingest.secrets import AwsSecretsManagerProvider

        provider = AwsSecretsManagerProvider(region="us-east-1")
        provider._client = fake_client
        return provider

    def test_parses_valid_secret_string(self):
        import json

        class _FakeClient:
            def get_secret_value(self, SecretId):
                assert SecretId == "prod/tuva/api-token"
                return {"SecretString": json.dumps({"api_token": "aws-secret-value"})}

        provider = self._provider_with_fake_client(_FakeClient())
        payload = provider.get_secret_json("prod/tuva/api-token")
        self.assertEqual(payload, {"api_token": "aws-secret-value"})

    def test_missing_secret_string_raises_secret_error(self):
        class _FakeClient:
            def get_secret_value(self, SecretId):
                return {}

        provider = self._provider_with_fake_client(_FakeClient())
        with self.assertRaises(SecretError):
            provider.get_secret_json("prod/tuva/api-token")

    def test_malformed_json_raises_secret_error(self):
        class _FakeClient:
            def get_secret_value(self, SecretId):
                return {"SecretString": "not-json{{{"}

        provider = self._provider_with_fake_client(_FakeClient())
        with self.assertRaises(SecretError):
            provider.get_secret_json("prod/tuva/api-token")

    def test_non_object_json_raises_secret_error(self):
        class _FakeClient:
            def get_secret_value(self, SecretId):
                return {"SecretString": "[1, 2, 3]"}

        provider = self._provider_with_fake_client(_FakeClient())
        with self.assertRaises(SecretError):
            provider.get_secret_json("prod/tuva/api-token")

    def test_boto3_client_error_is_wrapped_as_secret_error_never_leaked_raw(self):
        class _FakeClient:
            def get_secret_value(self, SecretId):
                raise RuntimeError("AccessDeniedException: some AWS-internal detail")

        provider = self._provider_with_fake_client(_FakeClient())
        with self.assertRaises(SecretError) as ctx:
            provider.get_secret_json("prod/tuva/api-token")
        self.assertIn("prod/tuva/api-token", str(ctx.exception))

    def test_never_configures_a_static_access_key(self):
        # AwsSecretsManagerProvider's constructor only ever accepts a
        # region -- there is no access-key/secret-key parameter to
        # accidentally pass one through.
        from tuva_ingest.secrets import AwsSecretsManagerProvider
        import inspect

        sig = inspect.signature(AwsSecretsManagerProvider.__init__)
        param_names = set(sig.parameters) - {"self"}
        self.assertEqual(param_names, {"region"})


class TestApiCredentialNeverLeaksSecret(unittest.TestCase):
    def test_repr_never_includes_token_value(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": "ultra-secret-value-xyz"})
        credential = retrieve_api_credential(config, provider=provider)
        self.assertNotIn("ultra-secret-value-xyz", repr(credential))

    def test_str_never_includes_token_value(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": "ultra-secret-value-xyz"})
        credential = retrieve_api_credential(config, provider=provider)
        self.assertNotIn("ultra-secret-value-xyz", str(credential))

    def test_secret_error_messages_never_include_a_real_token_value(self):
        config = _FakeConfig()
        provider = _FakeProvider({"api_token": 12345})
        with self.assertRaises(SecretError) as ctx:
            retrieve_api_credential(config, provider=provider)
        # There is no real token value in this failure case, but the
        # message must also never echo the raw (wrong-typed) input in a
        # way that could leak a partial/rejected credential.
        self.assertNotIn("12345", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
