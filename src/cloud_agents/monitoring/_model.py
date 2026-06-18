"""Model configuration for the monitoring agent pod."""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIResponsesModel

from pydantic_ai_lightspeed.llamastack import LlamaStackProvider

_model: OpenAIResponsesModel | None = None


def get_model() -> OpenAIResponsesModel:
    """Get or create the LLM model for the monitoring agent."""
    global _model
    if _model is None:
        base_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1")
        model_name = os.environ.get("AGENT_MODEL", "qwen3.6:latest")
        api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
        provider = LlamaStackProvider(base_url=base_url, api_key=api_key)
        _model = OpenAIResponsesModel(model_name, provider=provider)
    return _model
