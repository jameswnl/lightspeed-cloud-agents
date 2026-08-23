"""Cloud Agents tool registry -- public API and built-in tools.

Import this package to register built-in tools and access the tool API::

    from cloud_agents.tools import step_tool, register_tool, list_tools

Built-in tools are registered automatically when this package is imported.
If a tool module fails to import (e.g. missing dependency), it is skipped
with a warning — the rest of the package remains functional.
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

# Auto-discover and register built-in tools
_BUILTIN_MODULES = [
    "cloud_agents.tools.kubectl",
    "cloud_agents.tools.http_request",
    "cloud_agents.tools.read_file",
]

for _mod in _BUILTIN_MODULES:
    try:
        __import__(_mod)
    except Exception:
        logger.warning("Failed to load built-in tool module '%s'", _mod, exc_info=True)

__all__ = ["clear_tools", "list_tools", "register_tool", "step_tool"]
