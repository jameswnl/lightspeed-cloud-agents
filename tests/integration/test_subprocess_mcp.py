"""Integration: MCP server config passthrough to subprocess."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest


class TestSubprocessMCP:
    """Verify MCP server configs pass through to real subprocess."""

    @pytest.mark.asyncio
    async def test_child_receives_mcp_servers(self) -> None:
        """Real subprocess receives mcp_servers from stdin."""
        input_data = {
            "prompt": "test",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": [],
            "mcp_servers": [
                {"name": "test-server", "url": "http://localhost:99999/sse"},
            ],
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())
        # Should fail at MCP connection (no server running), NOT at
        # tool resolution — mcp_servers config was received and parsed.
        assert result["status"] == "failed"
        assert "Unknown tool" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_child_receives_mcp_with_headers(self) -> None:
        """Auth headers pass through without serialization errors."""
        input_data = {
            "prompt": "test",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": [],
            "mcp_servers": [
                {
                    "name": "auth-server",
                    "url": "http://localhost:99999/sse",
                    "headers": {"Authorization": "Bearer test-token"},
                },
            ],
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())
        # Should fail at connection, not at deserialization of headers.
        assert result["status"] == "failed"
        assert "Unknown tool" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_child_with_tools_and_mcp(self) -> None:
        """Combined tools + MCP servers in child process."""
        input_data = {
            "prompt": "test",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": ["echo_tool"],
            "tools_module": "tests.fixtures.sample_tools",
            "mcp_servers": [
                {"name": "test", "url": "http://localhost:99999/sse"},
            ],
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())
        # Should fail at MCP connection, not at tool resolution —
        # echo_tool should be found via tools_module.
        assert result["status"] == "failed"
        assert "Unknown tool" not in result.get("error", "")
