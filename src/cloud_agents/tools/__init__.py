"""Cloud Agents tool registry -- public API and built-in tools.

Import this package to register built-in tools and access the tool API::

    from cloud_agents.tools import step_tool, register_tool, list_tools

Built-in tools are registered automatically when this package is imported.
"""

from cloud_agents.workflow.executor.step.tools import (
    clear_tools,
    list_tools,
    register_tool,
    step_tool,
)

# Auto-discover and register built-in tools
from cloud_agents.tools import http_request, kubectl, read_file  # noqa: F401

__all__ = ["clear_tools", "list_tools", "register_tool", "step_tool"]
