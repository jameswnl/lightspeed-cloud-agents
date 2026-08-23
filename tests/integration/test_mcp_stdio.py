"""Integration test: MCP server via StdioTransport.

Creates a minimal MCP server as a Python script, connects via
StdioTransport, and proves that tools are discoverable. This validates
that the MCPToolset wiring works end-to-end without external infrastructure.

No real LLM is needed -- we just test tool discovery over stdio transport.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Minimal MCP server script that registers a single tool via FastMCP.
# The server runs in stdio mode, so it reads JSON-RPC from stdin and writes
# responses to stdout -- just like a real MCP server subprocess.
_MCP_SERVER_SCRIPT = '''\
"""Minimal MCP server for integration testing."""
from mcp.server.fastmcp import FastMCP

server = FastMCP("test-server")

@server.tool()
def echo(message: str) -> str:
    """Echo the message back."""
    return f"echo: {message}"

@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    server.run(transport="stdio")
'''


class TestMcpStdioTransport:
    """Verify MCPToolset with StdioTransport discovers tools from a real server."""

    @pytest.mark.asyncio
    async def test_mcp_stdio_tool_discovery(self) -> None:
        """MCPToolset with StdioTransport can connect and list tools.

        Starts a real MCP server as a subprocess via StdioTransport,
        connects to it, and verifies that both registered tools (echo, add)
        are discoverable via list_tools().
        """
        from pydantic_ai.mcp import MCPToolset, StdioTransport

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write(_MCP_SERVER_SCRIPT)
            script_path = f.name

        try:
            transport = StdioTransport(sys.executable, [script_path])
            toolset = MCPToolset(transport)

            async with toolset:
                tools = await toolset.list_tools()
                tool_names = [t.name for t in tools]

                assert "echo" in tool_names, f"Expected 'echo' tool, got: {tool_names}"
                assert "add" in tool_names, f"Expected 'add' tool, got: {tool_names}"
                assert len(tools) >= 2, f"Expected at least 2 tools, got {len(tools)}: {tool_names}"
        finally:
            os.unlink(script_path)

    @pytest.mark.asyncio
    async def test_mcp_stdio_tool_call(self) -> None:
        """MCPToolset with StdioTransport can call a tool and get results.

        Goes beyond discovery to actually invoke the echo tool via the
        MCP protocol over stdio, proving the full request/response cycle works.
        """
        from pydantic_ai.mcp import MCPToolset, StdioTransport

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write(_MCP_SERVER_SCRIPT)
            script_path = f.name

        try:
            transport = StdioTransport(sys.executable, [script_path])
            toolset = MCPToolset(transport)

            async with toolset:
                result = await toolset.direct_call_tool("echo", {"message": "hello"})

                # direct_call_tool returns the result content
                text = str(result)
                assert "hello" in text, f"Expected 'hello' in tool result, got: {text}"
        finally:
            os.unlink(script_path)

    @pytest.mark.asyncio
    async def test_mcp_stdio_multiple_tool_calls(self) -> None:
        """Multiple sequential tool calls work over the same transport.

        Ensures the stdio transport maintains a stable connection across
        multiple JSON-RPC round trips.
        """
        from pydantic_ai.mcp import MCPToolset, StdioTransport

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write(_MCP_SERVER_SCRIPT)
            script_path = f.name

        try:
            transport = StdioTransport(sys.executable, [script_path])
            toolset = MCPToolset(transport)

            async with toolset:
                # Call echo
                result1 = await toolset.direct_call_tool("echo", {"message": "first"})
                assert "first" in str(result1)

                # Call add
                result2 = await toolset.direct_call_tool("add", {"a": 3, "b": 4})
                assert "7" in str(result2)

                # Call echo again to verify the connection is still alive
                result3 = await toolset.direct_call_tool("echo", {"message": "third"})
                assert "third" in str(result3)
        finally:
            os.unlink(script_path)
