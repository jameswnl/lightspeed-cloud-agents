"""Integration test: subprocess child tool registry bootstrap.

Verifies that SubprocessExecutor can reconstruct the tool registry in a
real child process by importing a tools module specified via tools_module.

This test spawns an actual subprocess (not mocked) to catch the bootstrap
gap where _REGISTRY is empty in a fresh interpreter.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest


class TestSubprocessToolBootstrap:
    """Verify tool registry is reconstructed in real subprocess."""

    @pytest.mark.asyncio
    async def test_child_process_loads_tools_module(self) -> None:
        """Real subprocess can import tools_module and resolve tool names."""
        input_data = {
            "prompt": "Call echo_tool with message 'hello'",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "tools": ["echo_tool"],
            "tools_module": "tests.fixtures.sample_tools",
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=10,
        )

        # The child will fail at the LLM call (no real API key), but it
        # should NOT fail at get_tools() — the tools_module import should
        # have populated the registry.
        result = json.loads(stdout.decode())

        # If bootstrap failed, error would be "Unknown tool 'echo_tool'"
        if result["status"] == "failed":
            assert "Unknown tool" not in result.get("error", ""), (
                f"Tool registry bootstrap failed in child process: {result['error']}"
            )

    @pytest.mark.asyncio
    async def test_child_process_fails_without_tools_module(self) -> None:
        """Real subprocess fails to find tools when tools_module is not set."""
        input_data = {
            "prompt": "Call echo_tool",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "tools": ["echo_tool"],
            # No tools_module — registry will be empty
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=10,
        )

        result = json.loads(stdout.decode())

        assert result["status"] == "failed"
        assert "Unknown tool" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_child_process_no_tools_works_without_module(self) -> None:
        """Real subprocess works without tools_module when no tools requested."""
        input_data = {
            "prompt": "Say hello",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "tools": [],
            # No tools_module needed when no tools
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=10,
        )

        result = json.loads(stdout.decode())

        # Will fail at LLM call (no API key), but NOT at tool resolution
        if result["status"] == "failed":
            assert "Unknown tool" not in result.get("error", "")
