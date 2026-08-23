"""Shared fixtures for tools tests."""

from __future__ import annotations

import sys

import pytest

from cloud_agents.workflow.executor.step.tools import clear_tools

# Tool modules whose @step_tool decorators fire on first import.
# We remove them from sys.modules so each test can trigger a fresh import.
_TOOL_MODULES = [
    "cloud_agents.tools",
    "cloud_agents.tools.kubectl",
    "cloud_agents.tools.http_request",
    "cloud_agents.tools.read_file",
]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear tool registry and module cache before/after each test."""
    clear_tools()
    for mod in _TOOL_MODULES:
        sys.modules.pop(mod, None)
    yield
    clear_tools()
    for mod in _TOOL_MODULES:
        sys.modules.pop(mod, None)
