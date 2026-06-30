"""MCP server loader for agent runtime.

Resolves MCPServerSpec entries into pydantic-ai MCPServerHTTP instances.
Auth tokens are resolved from environment variables (production) or
inline values (dev/test only, with startup warning).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from cloud_agents.definition import MCPServerSpec

logger = logging.getLogger(__name__)


def resolve_mcp_headers(spec: MCPServerSpec) -> dict[str, str]:
    """Resolve authentication headers from an MCP server spec.

    Args:
        spec: MCP server specification with optional auth config.

    Returns:
        Dict of HTTP headers for the MCP connection.

    Raises:
        ValueError: If env_var auth references an unset variable.
    """
    if not spec.auth:
        return {}

    auth = spec.auth
    if auth.type == "env_var":
        if not auth.env_var:
            raise ValueError(
                f"MCP server '{spec.name}': env_var auth requires env_var field"
            )
        token = os.environ.get(auth.env_var)
        if not token:
            raise ValueError(
                f"MCP server '{spec.name}': environment variable "
                f"'{auth.env_var}' is not set"
            )
        return {auth.header_name: f"{auth.header_prefix}{token}"}

    if auth.type == "header_value":
        if not auth.header_value:
            raise ValueError(
                f"MCP server '{spec.name}': header_value auth requires header_value field"
            )
        logger.warning(
            "MCP server '%s': using inline header_value auth — dev/test only. "
            "Use env_var auth in production.",
            spec.name,
        )
        return {auth.header_name: f"{auth.header_prefix}{auth.header_value}"}

    return {}


def load_mcp_servers(specs: list[MCPServerSpec]) -> list[Any]:
    """Load MCP server instances from specifications.

    Args:
        specs: List of MCP server specifications.

    Returns:
        List of MCPServerHTTP instances for pydantic-ai Agent.
    """
    from pydantic_ai.mcp import MCPToolset

    servers = []
    for spec in specs:
        headers = resolve_mcp_headers(spec)
        server = MCPToolset(spec.url, headers=headers if headers else None)
        servers.append(server)
        logger.info("Loaded MCP server: %s (%s)", spec.name, spec.url)
    return servers
