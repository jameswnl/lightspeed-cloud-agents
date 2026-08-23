"""Integration: MCP server via StdioTransport."""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# The MCP server script uses mcp.server.fastmcp (the real server library),
# not pydantic_ai.mcp (the client-side re-export).
MCP_SERVER_SCRIPT = '''\
from mcp.server.fastmcp import FastMCP

server = FastMCP("test-server")


@server.tool()
def echo(message: str) -> str:
    """Echo back."""
    return f"echo: {message}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    server.run()
'''


class TestMCPStdio:
    """Verify MCP tool discovery and calls over real stdio transport."""

    @pytest.mark.asyncio
    async def test_mcp_stdio_tool_discovery(self) -> None:
        """MCPToolset discovers tools from a real stdio MCP server."""
        from pydantic_ai.mcp import MCPToolset, StdioTransport

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(MCP_SERVER_SCRIPT)
            script_path = f.name

        try:
            transport = StdioTransport(command=sys.executable, args=[script_path])
            async with MCPToolset(transport) as toolset:
                tool_defs = await toolset.list_tools()
                names = [t.name for t in tool_defs]
                assert "echo" in names
                assert "add" in names
        finally:
            os.unlink(script_path)

    @pytest.mark.asyncio
    async def test_mcp_stdio_tool_call(self) -> None:
        """Can call a tool on a real stdio MCP server."""
        from pydantic_ai.mcp import MCPToolset, StdioTransport

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(MCP_SERVER_SCRIPT)
            script_path = f.name

        try:
            transport = StdioTransport(command=sys.executable, args=[script_path])
            async with MCPToolset(transport) as toolset:
                result = await toolset.direct_call_tool("echo", {"message": "hello"})
                assert "echo: hello" in str(result)
        finally:
            os.unlink(script_path)

    @pytest.mark.asyncio
    async def test_mcp_stdio_multiple_calls(self) -> None:
        """Multiple calls over same stdio connection."""
        from pydantic_ai.mcp import MCPToolset, StdioTransport

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(MCP_SERVER_SCRIPT)
            script_path = f.name

        try:
            transport = StdioTransport(command=sys.executable, args=[script_path])
            async with MCPToolset(transport) as toolset:
                r1 = await toolset.direct_call_tool("echo", {"message": "first"})
                r2 = await toolset.direct_call_tool("add", {"a": 2, "b": 3})
                r3 = await toolset.direct_call_tool("echo", {"message": "third"})
                assert "first" in str(r1)
                assert "5" in str(r2)
                assert "third" in str(r3)
        finally:
            os.unlink(script_path)
