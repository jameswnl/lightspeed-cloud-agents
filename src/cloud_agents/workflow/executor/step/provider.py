"""Shared provider mapping and credential resolution for step executors.

Maps workflow provider config dicts to pydantic-ai model strings and
ensures credentials are available in the environment.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PROVIDER_NAME_MAP: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "google-gla",
    "azure": "openai",
    "bedrock": "bedrock",
}

_PROVIDER_ENV_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
}


def to_model_string(provider: dict[str, Any]) -> str:
    """Convert workflow provider dict to pydantic-ai model string.

    Parameters:
        provider: Provider config with 'name' and 'model' keys.

    Returns:
        Model string like "openai:gpt-4o" or "anthropic:claude-sonnet-5".

    Raises:
        ValueError: If provider name is unknown.
    """
    name = provider["name"]
    model = provider["model"]
    pai_provider = _PROVIDER_NAME_MAP.get(name)
    if pai_provider is None:
        raise ValueError(
            f"Unknown provider '{name}'. "
            f"Supported: {', '.join(sorted(_PROVIDER_NAME_MAP))}."
        )
    return f"{pai_provider}:{model}"


def resolve_api_key(provider: dict[str, Any]) -> str | None:
    """Resolve API key from provider config and environment.

    Resolution order:
    1. credentials_secret normalized to UPPER_SNAKE env var
    2. Provider-specific default env var (e.g., OPENAI_API_KEY)

    Parameters:
        provider: Provider config dict.

    Returns:
        API key string, or None if not found.
    """
    cred = provider.get("credentials_secret")
    if cred:
        env_key = cred.upper().replace("-", "_")
        val = os.environ.get(env_key)
        if val:
            return val

    name = provider.get("name", "")
    default_key = _PROVIDER_ENV_KEYS.get(name)
    if default_key:
        return os.environ.get(default_key)

    return None


def ensure_credentials_env(provider: dict[str, Any]) -> None:
    """Ensure the provider's API key and endpoint are set for pydantic-ai.

    pydantic-ai reads credentials from well-known env vars (OPENAI_API_KEY,
    ANTHROPIC_API_KEY, etc.). This function resolves the key from the workflow
    provider config and sets it if not already present.

    For Azure, also sets AZURE_OPENAI_ENDPOINT from the provider's base_url.

    Note: This mutates os.environ. First-key-wins — if the env var is already
    set, it is not overwritten. This is intentional: pydantic-ai has no API to
    pass credentials directly, so env vars are the only injection point.

    Parameters:
        provider: Provider config dict.
    """
    name = provider.get("name", "")
    target_env = _PROVIDER_ENV_KEYS.get(name)
    if not target_env:
        return

    if os.environ.get(target_env):
        return

    api_key = resolve_api_key(provider)
    if api_key:
        os.environ[target_env] = api_key
        logger.debug("Set %s from credentials_secret for pydantic-ai", target_env)

    if name == "azure":
        base_url = provider.get("base_url")
        if base_url and not os.environ.get("AZURE_OPENAI_ENDPOINT"):
            os.environ["AZURE_OPENAI_ENDPOINT"] = base_url
            logger.debug("Set AZURE_OPENAI_ENDPOINT from provider base_url")
