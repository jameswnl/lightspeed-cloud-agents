"""Sample tools module for testing subprocess tool bootstrap.

When imported, this module registers tools via @step_tool decorator.
Used by integration tests to verify that subprocess_child can reconstruct
the tool registry by importing a tools module.
"""

from __future__ import annotations

from cloud_agents.workflow.executor.step.tools import step_tool


@step_tool("echo_tool", description="Returns the input as-is")
def echo_tool(message: str) -> str:
    """Echo the input message back."""
    return f"echo: {message}"


@step_tool("add_numbers", description="Add two numbers")
def add_numbers(a: int, b: int) -> int:
    """Add two numbers and return the result."""
    return a + b
