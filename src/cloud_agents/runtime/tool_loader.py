"""Tool loader — imports Python modules and extracts tool functions.

Loads tool functions by module name and function name for registration
with a Pydantic AI Agent via agent.tool_plain().
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from agents.definition import ToolsSpec


class ToolLoadError(Exception):
    """Raised when a tool module or function cannot be loaded."""


def load_tools(spec: ToolsSpec) -> list[tuple[str, Callable[..., Any]]]:
    """Load tool functions from a Python module.

    Args:
        spec: Tool specification with module name and function names.

    Returns:
        List of (name, callable) pairs ready for agent.tool_plain().

    Raises:
        ToolLoadError: If the module or any function cannot be loaded.
    """
    try:
        module = importlib.import_module(spec.module)
    except ImportError as exc:
        raise ToolLoadError(
            f"Cannot import tool module '{spec.module}': {exc}"
        ) from exc

    tools: list[tuple[str, Callable[..., Any]]] = []
    for fn_name in spec.functions:
        fn = getattr(module, fn_name, None)
        if fn is None:
            raise ToolLoadError(
                f"Function '{fn_name}' not found in module '{spec.module}'"
            )
        if not callable(fn):
            raise ToolLoadError(
                f"'{fn_name}' in '{spec.module}' is not callable"
            )
        tools.append((fn_name, fn))
    return tools
