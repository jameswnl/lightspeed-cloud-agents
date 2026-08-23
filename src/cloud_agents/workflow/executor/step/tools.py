"""Tool registry for spawn: none and spawn: local step execution.

Maps tool name strings to pydantic-ai Tool instances. Tools are registered
at startup and filtered per step based on the step's tools list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic_ai import Tool

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Tool] = {}


def register_tool(
    name: str,
    func: Callable[..., Any],
    *,
    description: str | None = None,
) -> None:
    """Register a tool function under the given name.

    Parameters:
        name: Tool name used in workflow step definitions.
        func: The tool function.
        description: Optional description (defaults to func docstring).

    Raises:
        ValueError: If a tool with this name is already registered.
    """
    if name in _REGISTRY:
        raise ValueError(f"Tool '{name}' is already registered.")
    _REGISTRY[name] = Tool(func, name=name, description=description)
    logger.info("Registered tool '%s'", name)


def get_tools(names: list[str]) -> list[Tool]:
    """Return pydantic-ai Tool instances for the given names.

    Parameters:
        names: List of tool name strings.

    Returns:
        List of Tool instances.

    Raises:
        ValueError: If any name is not registered.
    """
    tools = []
    for name in names:
        if name not in _REGISTRY:
            available = ", ".join(sorted(_REGISTRY)) or "(none)"
            raise ValueError(f"Unknown tool '{name}'. Registered tools: {available}.")
        tools.append(_REGISTRY[name])
    return tools


def step_tool(
    name: str,
    *,
    description: str | None = None,
) -> Callable:
    """Decorator to register a function as a step tool.

    Parameters:
        name: Tool name used in workflow step definitions.
        description: Optional description.

    Returns:
        The original function (unmodified).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        register_tool(name, func, description=description)
        return func

    return decorator


def list_tools() -> list[str]:
    """List all registered tool names.

    Returns:
        Sorted list of tool name strings.
    """
    return sorted(_REGISTRY)


def clear_tools() -> None:
    """Remove all registered tools. For testing only."""
    _REGISTRY.clear()
