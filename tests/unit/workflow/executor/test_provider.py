"""Tests for shared provider mapping and credential resolution."""

from __future__ import annotations

import os

import pytest
from pytest_mock import MockerFixture


class TestToModelString:
    """Tests for to_model_string() provider-to-pydantic-ai mapping."""

    def test_openai_provider(self) -> None:
        """OpenAI provider maps to openai: prefix."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        result = to_model_string({"name": "openai", "model": "gpt-4o"})
        assert result == "openai:gpt-4o"

    def test_anthropic_provider(self) -> None:
        """Anthropic provider maps to anthropic: prefix."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        result = to_model_string({"name": "anthropic", "model": "claude-sonnet-5"})
        assert result == "anthropic:claude-sonnet-5"

    def test_gemini_provider(self) -> None:
        """Gemini provider maps to google-gla: prefix (pydantic-ai convention)."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        result = to_model_string({"name": "gemini", "model": "gemini-2.5-pro"})
        assert result == "google-gla:gemini-2.5-pro"

    def test_azure_provider(self) -> None:
        """Azure provider maps to azure: prefix (native pydantic-ai Azure support)."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        result = to_model_string({"name": "azure", "model": "gpt-4o"})
        assert result == "azure:gpt-4o"

    def test_bedrock_provider(self) -> None:
        """Bedrock provider maps to bedrock: prefix."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        result = to_model_string({"name": "bedrock", "model": "us.anthropic.claude-sonnet-5"})
        assert result == "bedrock:us.anthropic.claude-sonnet-5"

    def test_claude_alias(self) -> None:
        """'claude' is an alias for 'anthropic'."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        result = to_model_string({"name": "claude", "model": "claude-sonnet-5"})
        assert result == "anthropic:claude-sonnet-5"

    def test_unknown_provider_raises(self) -> None:
        """Unknown provider raises ValueError with supported list."""
        from cloud_agents.workflow.executor.step.provider import to_model_string

        with pytest.raises(ValueError, match="Unknown provider 'llama'"):
            to_model_string({"name": "llama", "model": "llama-3"})


class TestResolveApiKey:
    """Tests for resolve_api_key() credential resolution."""

    def test_credentials_secret_normalized(self, mocker: MockerFixture) -> None:
        """credentials_secret is normalized to UPPER_SNAKE env var."""
        from cloud_agents.workflow.executor.step.provider import resolve_api_key

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-123"}, clear=False)

        key = resolve_api_key({"name": "openai", "credentials_secret": "openai-api-key"})
        assert key == "sk-test-123"

    def test_custom_credentials_secret(self, mocker: MockerFixture) -> None:
        """Custom credentials_secret name is resolved from env."""
        from cloud_agents.workflow.executor.step.provider import resolve_api_key

        mocker.patch.dict(os.environ, {"MY_CUSTOM_KEY": "sk-custom"}, clear=False)

        key = resolve_api_key({"name": "openai", "credentials_secret": "my-custom-key"})
        assert key == "sk-custom"

    def test_falls_back_to_provider_default(self, mocker: MockerFixture) -> None:
        """Falls back to provider-specific default env var."""
        from cloud_agents.workflow.executor.step.provider import resolve_api_key

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-default"}, clear=False)

        key = resolve_api_key({"name": "openai"})
        assert key == "sk-default"

    def test_anthropic_fallback(self, mocker: MockerFixture) -> None:
        """Anthropic provider falls back to ANTHROPIC_API_KEY."""
        from cloud_agents.workflow.executor.step.provider import resolve_api_key

        mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant"}, clear=False)

        key = resolve_api_key({"name": "anthropic"})
        assert key == "sk-ant"

    def test_returns_none_if_not_found(self, mocker: MockerFixture) -> None:
        """Returns None if no API key is found."""
        from cloud_agents.workflow.executor.step.provider import resolve_api_key

        env_copy = {
            k: v for k, v in os.environ.items() if "OPENAI" not in k and "ANTHROPIC" not in k
        }
        mocker.patch.dict(os.environ, env_copy, clear=True)

        key = resolve_api_key({"name": "openai"})
        assert key is None

    def test_credentials_secret_takes_priority(self, mocker: MockerFixture) -> None:
        """credentials_secret takes priority over provider default."""
        from cloud_agents.workflow.executor.step.provider import resolve_api_key

        mocker.patch.dict(
            os.environ,
            {"MY_KEY": "sk-custom", "OPENAI_API_KEY": "sk-default"},
            clear=False,
        )

        key = resolve_api_key({"name": "openai", "credentials_secret": "my-key"})
        assert key == "sk-custom"


class TestEnsureCredentialsEnv:
    """Tests for ensure_credentials_env() env var setup."""

    def test_sets_openai_key(self, mocker: MockerFixture) -> None:
        """Sets OPENAI_API_KEY for openai provider from credentials_secret."""
        from cloud_agents.workflow.executor.step.provider import ensure_credentials_env

        env_copy = {k: v for k, v in os.environ.items() if "OPENAI" not in k}
        env_copy["MY_API_KEY"] = "sk-from-secret"
        mocker.patch.dict(os.environ, env_copy, clear=True)

        ensure_credentials_env({"name": "openai", "credentials_secret": "my-api-key"})

        assert os.environ.get("OPENAI_API_KEY") == "sk-from-secret"

    def test_sets_anthropic_key(self, mocker: MockerFixture) -> None:
        """Sets ANTHROPIC_API_KEY for anthropic provider."""
        from cloud_agents.workflow.executor.step.provider import ensure_credentials_env

        env_copy = {k: v for k, v in os.environ.items() if "ANTHROPIC" not in k}
        env_copy["MY_ANT_KEY"] = "sk-ant-from-secret"
        mocker.patch.dict(os.environ, env_copy, clear=True)

        ensure_credentials_env({"name": "anthropic", "credentials_secret": "my-ant-key"})

        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-from-secret"

    def test_does_not_overwrite_existing(self, mocker: MockerFixture) -> None:
        """Does not overwrite existing env var."""
        from cloud_agents.workflow.executor.step.provider import ensure_credentials_env

        mocker.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-existing"},
            clear=False,
        )

        ensure_credentials_env({"name": "openai", "credentials_secret": "other-key"})

        assert os.environ.get("OPENAI_API_KEY") == "sk-existing"

    def test_unknown_provider_no_op(self, mocker: MockerFixture) -> None:
        """Unknown provider with no target env var is a no-op."""
        from cloud_agents.workflow.executor.step.provider import ensure_credentials_env

        # Should not raise
        ensure_credentials_env({"name": "bedrock", "credentials_secret": "k"})

    def test_azure_sets_endpoint_env(self, mocker: MockerFixture) -> None:
        """Azure provider sets AZURE_OPENAI_ENDPOINT from base_url."""
        from cloud_agents.workflow.executor.step.provider import ensure_credentials_env

        env_copy = {k: v for k, v in os.environ.items()
                    if k not in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")}
        env_copy["MY_AZURE_KEY"] = "sk-azure-123"
        mocker.patch.dict(os.environ, env_copy, clear=True)

        ensure_credentials_env({
            "name": "azure",
            "credentials_secret": "my-azure-key",
            "base_url": "https://myorg.openai.azure.com",
        })

        assert os.environ.get("AZURE_OPENAI_API_KEY") == "sk-azure-123"
        assert os.environ.get("AZURE_OPENAI_ENDPOINT") == "https://myorg.openai.azure.com"

    def test_azure_does_not_overwrite_endpoint(self, mocker: MockerFixture) -> None:
        """Azure does not overwrite existing AZURE_OPENAI_ENDPOINT."""
        from cloud_agents.workflow.executor.step.provider import ensure_credentials_env

        mocker.patch.dict(os.environ, {
            "AZURE_OPENAI_API_KEY": "sk-existing",
            "AZURE_OPENAI_ENDPOINT": "https://existing.azure.com",
        }, clear=False)

        ensure_credentials_env({
            "name": "azure",
            "base_url": "https://new.azure.com",
        })

        assert os.environ.get("AZURE_OPENAI_ENDPOINT") == "https://existing.azure.com"
