"""Cloud Agents tool registry -- public API and built-in tools.

Import this package to access the tool API::

    from cloud_agents.tools import step_tool, register_tool, list_tools

Built-in tools are NOT registered automatically on import. Call
``load_builtin_tools()`` explicitly at startup to register them.
"""

from __future__ import annotations

import logging

from cloud_agents.workflow.executor.step.tools import (
    clear_tools,
    list_tools,
    register_tool,
    step_tool,
)

logger = logging.getLogger(__name__)

# Built-in tool modules to load on demand
_BUILTIN_MODULES = [
    "cloud_agents.tools.kubectl",
    "cloud_agents.tools.http_request",
    "cloud_agents.tools.read_file",
]

_BUILTINS_LOADED = False


def load_builtin_tools() -> None:
    """Explicitly load built-in tools. Call at startup when cloud_agents is enabled.

    This function is idempotent -- calling it multiple times has no effect
    after the first successful call.
    """
    global _BUILTINS_LOADED  # noqa: PLW0603
    if _BUILTINS_LOADED:
        return
    for _mod in _BUILTIN_MODULES:
        try:
            __import__(_mod)
        except Exception:
            logger.warning("Failed to load built-in tool module '%s'", _mod, exc_info=True)
    _BUILTINS_LOADED = True


__all__ = [
    "clear_tools",
    "list_tools",
    "load_builtin_tools",
    "register_tool",
    "step_tool",
]
