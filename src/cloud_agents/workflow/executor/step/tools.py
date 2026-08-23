"""Tool registry for step execution across all spawn modes.

Product teams register Python functions as tools via register_tool() or
the @step_tool decorator. The workflow engine resolves tool names at
runtime based on the step's spawn mode.

Public API (for product teams):
    register_tool(name, func) — register a callable as a named tool
    step_tool(name) — decorator form of register_tool
    list_tools() — list registered tool names
    clear_tools() — remove all (testing only)

Internal API (for executors):
    get_tools(names) — resolve names to pydantic-ai Tool objects
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDefinition:
    """Internal representation of a registered tool.

    Attributes:
        name: Tool name used in workflow step definitions.
        func: The tool function.
        description: Human-readable description for the LLM.
    """

    name: str
    func: Callable[..., Any]
    description: str | None = None


_REGISTRY: dict[str, ToolDefinition] = {}


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
    _REGISTRY[name] = ToolDefinition(name=name, func=func, description=description)
    logger.info("Registered tool '%s'", name)


def get_tools(names: list[str]) -> list[Any]:
    """Resolve tool names to pydantic-ai Tool instances.

    This is an internal API used by DirectExecutor and subprocess_child.
    Product teams should not call this directly.

    Parameters:
        names: List of tool name strings.

    Returns:
        List of pydantic-ai Tool instances.

    Raises:
        ValueError: If any name is not registered.
    """
    from pydantic_ai import Tool

    tools = []
    for name in names:
        if name not in _REGISTRY:
            available = ", ".join(sorted(_REGISTRY)) or "(none)"
            hint = (
                " Set CLOUD_AGENTS_TOOLS_MODULE to the dotted import path of "
                "your tools module (e.g. myapp.tools)."
                if available == "(none)"
                else ""
            )
            raise ValueError(
                f"Unknown tool '{name}'. Registered tools: {available}.{hint}"
            )
        defn = _REGISTRY[name]
        tools.append(Tool(defn.func, name=defn.name, description=defn.description))
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


def load_tools_module(module_path: str) -> None:
    """Import a module to trigger @step_tool registrations.

    Used by subprocess_child to reconstruct the tool registry in the
    child process. The module is imported via importlib, which runs
    any @step_tool decorators at module scope.

    Parameters:
        module_path: Dotted Python import path (e.g. 'myapp.tools').
    """
    import importlib

    importlib.import_module(module_path)
    logger.debug("Loaded tools module '%s'", module_path)


def clear_tools() -> None:
    """Remove all registered tools. For testing only."""
    _REGISTRY.clear()
