"""Integration test: subprocess receives and processes MCP server configs.

Proves that MCP server configurations are passed through to the child
process via stdin and parsed correctly. The MCP connection itself will
fail (no real server running) but the error should be about connection,
not about missing fields or KeyErrors in the MCP config handling.

This catches breaks in the subprocess_child MCP wiring path that unit
tests with mocked MCPToolset cannot detect.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest


class TestSubprocessMcpPassthrough:
    """Verify MCP server configs are properly received by child process."""

    @pytest.mark.asyncio
    async def test_child_receives_mcp_servers(self) -> None:
        """Real subprocess receives and attempts to use mcp_servers from stdin.

        The child should fail with a connection error (no server at
        localhost:99999), NOT with a KeyError, attribute error, or
        'mcp_servers not recognized' error. This proves the MCP config
        deserialization and MCPToolset construction paths work.
        """
        input_data = {
            "prompt": "test mcp passthrough",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": [],
            "mcp_servers": [
                {
                    "name": "test-server",
                    "url": "http://localhost:99999/sse",
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

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())

        # Should fail (no server) but NOT with a deserialization error
        assert result["status"] == "failed"
        error = result.get("error", "")

        # Error should be about connection/network, not about missing fields
        assert (
            "Unknown tool" not in error
        ), f"MCP wiring broken -- tools error instead of connection: {error}"
        assert "KeyError" not in error, f"MCP config not properly handled in child: {error}"
        assert "AttributeError" not in error, f"MCP object wiring broken in child: {error}"

    @pytest.mark.asyncio
    async def test_child_receives_mcp_servers_with_headers(self) -> None:
        """MCP server config with auth headers is passed through to child.

        Verifies that the headers field (used for MCP auth) does not cause
        serialization or deserialization errors across the process boundary.
        """
        input_data = {
            "prompt": "test mcp with auth",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": [],
            "mcp_servers": [
                {
                    "name": "auth-server",
                    "url": "http://localhost:99999/mcp",
                    "headers": {
                        "Authorization": "Bearer test-token-123",
                        "X-Custom-Header": "value",
                    },
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

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())

        # Should fail with connection error, not serialization error
        assert result["status"] == "failed"
        error = result.get("error", "")
        assert "KeyError" not in error, f"Header passthrough broken in child: {error}"
        assert "TypeError" not in error, f"Header serialization broken in child: {error}"

    @pytest.mark.asyncio
    async def test_child_with_tools_and_mcp_servers(self) -> None:
        """Child process handles both tools and mcp_servers simultaneously.

        When a step has both registry tools and MCP servers, the child
        should construct both the tool list and MCPToolsets. This test
        verifies that the combined path works without conflicts.
        """
        input_data = {
            "prompt": "test combined tools and mcp",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": ["kubectl_get"],
            "tools_module": "cloud_agents.tools",
            "mcp_servers": [
                {
                    "name": "test-server",
                    "url": "http://localhost:99999/sse",
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

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())

        # Should fail at MCP connection (no server), not at tool resolution
        assert result["status"] == "failed"
        error = result.get("error", "")
        assert (
            "Unknown tool" not in error
        ), f"Tool resolution failed despite tools_module in combined mode: {error}"
