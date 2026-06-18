"""Model configuration for the diagnostic agent pod.

Reads LLM backend configuration from environment variables.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIResponsesModel

from pydantic_ai_lightspeed.llamastack import LlamaStackProvider

_model: OpenAIResponsesModel | None = None


def get_model() -> OpenAIResponsesModel:
    """Get or create the LLM model for the diagnostic agent.

    Reads configuration from environment variables:
    - OLLAMA_URL: Base URL for the LLM backend (default: http://localhost:11434/v1)
    - AGENT_MODEL: Model name (default: qwen3.6:latest)
    - OPENAI_API_KEY: API key (default: not-needed, for local Ollama)

    Returns:
        Configured OpenAIResponsesModel.
    """
    global _model
    if _model is None:
        base_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1")
        model_name = os.environ.get("AGENT_MODEL", "qwen3.6:latest")
        api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
        provider = LlamaStackProvider(base_url=base_url, api_key=api_key)
        _model = OpenAIResponsesModel(model_name, provider=provider)
    return _model
