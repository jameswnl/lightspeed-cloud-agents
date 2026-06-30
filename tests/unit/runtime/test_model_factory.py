"""Unit tests for shared model factory."""

import os
from unittest.mock import patch

from agents.runtime.model_factory import get_model, reset_model


class TestGetModel:
    """Tests for get_model factory."""

    def setup_method(self) -> None:
        """Reset cached model between tests."""
        reset_model()

    def test_default_values(self) -> None:
        """Test that defaults produce a valid model."""
        model = get_model()
        assert model is not None

    def test_model_is_cached(self) -> None:
        """Test that repeated calls return the same instance."""
        m1 = get_model()
        m2 = get_model()
        assert m1 is m2

    def test_reset_clears_cache(self) -> None:
        """Test that reset allows creating a new model."""
        m1 = get_model()
        reset_model()
        m2 = get_model()
        assert m1 is not m2

    def test_explicit_overrides(self) -> None:
        """Test that explicit params override env vars."""
        model = get_model(
            model_name="test-model",
            base_url="http://test:9999/v1",
            api_key="test-key",
        )
        assert model is not None

    def test_env_var_model_name(self) -> None:
        """Test AGENT_MODEL env var."""
        with patch.dict(os.environ, {"AGENT_MODEL": "custom-model"}):
            model = get_model()
            assert model is not None

    def test_env_var_api_key(self) -> None:
        """Test OPENAI_API_KEY env var."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            model = get_model()
            assert model is not None
