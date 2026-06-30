"""Shared model factory for agent pods.

Creates OpenAIResponsesModel from environment variables.
Replaces the duplicated _model.py in each agent package.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIResponsesModel

from pydantic_ai_lightspeed.llamastack import LlamaStackProvider

_model: OpenAIResponsesModel | None = None


def get_model(
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> OpenAIResponsesModel:
    """Get or create the LLM model.

    Reads from env vars with optional explicit overrides.

    Args:
        model_name: Override for AGENT_MODEL env var.
        base_url: Override for OLLAMA_URL env var.
        api_key: Override for OPENAI_API_KEY env var.

    Returns:
        Configured OpenAIResponsesModel (cached on first call).
    """
    global _model
    if _model is None:
        _base_url = base_url or os.environ.get(
            "OLLAMA_URL", "http://localhost:11434/v1"
        )
        _model_name = model_name or os.environ.get("AGENT_MODEL", "qwen3.6:latest")
        _api_key = api_key or os.environ.get("OPENAI_API_KEY", "not-needed")
        provider = LlamaStackProvider(base_url=_base_url, api_key=_api_key)
        _model = OpenAIResponsesModel(_model_name, provider=provider)
    return _model


def reset_model() -> None:
    """Reset the cached model. Used for testing."""
    global _model
    _model = None
