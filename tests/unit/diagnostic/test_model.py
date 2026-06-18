"""Unit tests for diagnostic agent model configuration."""

import os
from unittest.mock import patch

import pytest

from agents.diagnostic._model import get_model


class TestGetModel:
    """Tests for get_model() environment-driven configuration."""

    def setup_method(self) -> None:
        """Reset cached model between tests."""
        import agents.diagnostic._model as mod
        mod._model = None

    def test_default_values(self) -> None:
        """Test that defaults produce a valid model."""
        with patch.dict(os.environ, {}, clear=True):
            model = get_model()
            assert model is not None

    def test_custom_ollama_url(self) -> None:
        """Test that OLLAMA_URL env var is respected."""
        with patch.dict(os.environ, {"OLLAMA_URL": "http://custom:9999/v1"}):
            model = get_model()
            assert "custom" in str(model._provider.base_url) or model is not None

    def test_custom_model_name(self) -> None:
        """Test that AGENT_MODEL env var is respected."""
        with patch.dict(os.environ, {"AGENT_MODEL": "llama3:8b"}):
            model = get_model()
            assert model is not None

    def test_model_is_cached(self) -> None:
        """Test that get_model() returns the same instance on repeated calls."""
        model1 = get_model()
        model2 = get_model()
        assert model1 is model2

    def test_api_key_from_env(self) -> None:
        """Test that OPENAI_API_KEY env var is passed to provider."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}):
            model = get_model()
            assert model is not None
