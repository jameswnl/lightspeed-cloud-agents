"""Shared MCP server resolver for all spawn modes.

Resolves a step's ``mcp_servers`` declaration (which may contain
reference-by-name strings and/or inline MCPServerConfig dicts) against
the run-level catalog.

Used by all three spawn modes so that least-privilege filtering is
consistent regardless of ``spawn:``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _to_dict(server: Any) -> dict[str, Any]:
    """Normalise a catalog or inline entry to a plain dict.

    Handles both plain dicts and Pydantic BaseModel instances
    (MCPServerConfig). Nested BaseModels (SecretHeaderRef) are
    recursively converted via ``model_dump``.
    """
    # Use duck-typing for BaseModel to avoid hard import dependency.
    if hasattr(server, "model_dump"):
        return server.model_dump()  # type: ignore[no-any-return]
    if isinstance(server, dict):
        return server
    # Fallback: unknown shape - return empty (dict case already handled above)
    return {}


def resolve_mcp_servers(
    step_mcp_servers: Optional[list[Any]],
    catalog: Optional[list[Any]],
) -> Optional[list[dict[str, Any]]]:
    """Resolve step mcp_servers against the run catalog.

    Parameters:
        step_mcp_servers: The step's ``mcp_servers`` field. Each entry
            may be a ``str`` (catalog reference) or a ``dict`` /
            ``MCPServerConfig`` (inline definition).
        catalog: The run-level catalog (``input["mcp_servers"]`` or
            ``WorkflowInput.mcp_servers``). List of dicts or
            MCPServerConfig with at least ``name`` and ``url``.

    Returns:
        Resolved list of server dicts, or ``None`` when the step
        requested no servers. The ``None`` vs ``[]`` distinction is
        preserved for callers that check ``if raw_mcp_servers:`` --
        both are falsy but ``None`` matches the historic ephemeral
        ``raw_mcp_servers = None`` sentinel for "no filtering needed".

        An empty or ``None`` step list yields ``None`` (no servers).
        Unknown string references are dropped with a warning.
        Inline configs are returned verbatim (normalised to dict).

    Example:
        >>> resolve_mcp_servers(["a"], [{"name": "a", "url": "http://a"}])
        [{'name': 'a', 'url': 'http://a'}]
        >>> resolve_mcp_servers([{"name": "inline", "url": "http://x"}], [])
        [{'name': 'inline', 'url': 'http://x'}]
    """
    if not step_mcp_servers:
        return None

    catalog = catalog or []
    # Build name -> dict index for string references
    mcp_by_name: dict[str, dict[str, Any]] = {}
    for entry in catalog:
        d = _to_dict(entry)
        name = d.get("name")
        if isinstance(name, str) and name:
            mcp_by_name[name] = d

    resolved: list[dict[str, Any]] = []
    for item in step_mcp_servers:
        if isinstance(item, str):
            server = mcp_by_name.get(item)
            if server is not None:
                resolved.append(server)
            else:
                logger.warning(
                    "MCP server '%s' not found in catalog -- skipping", item
                )
        elif isinstance(item, dict):
            # Inline dict definition -- validated at submission time
            # (validate_definition) so incomplete entries are rejected
            # with 422 before reaching the resolver.
            resolved.append(dict(item))
        elif hasattr(item, "model_dump"):
            # Inline MCPServerConfig model instance
            resolved.append(item.model_dump())  # type: ignore[union-attr]
        else:
            logger.warning("Unknown mcp_servers entry type %r -- skipping", item)

    if not resolved:
        # No valid servers resolved -- treat as "no servers" to keep
        # callers' ``if raw_mcp_servers:`` guards equivalent to the
        # historic "None means no injection" semantics.
        # But distinguish: if step asked for names that all missed, it
        # should get no servers (None), not the whole catalog.
        return None

    return resolved
