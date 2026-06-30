"""Unit tests for MCP server loader."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cloud_agents.definition import MCPAuthSpec, MCPServerSpec
from cloud_agents.runtime.mcp_loader import load_mcp_servers, resolve_mcp_headers


class TestResolveHeaders:
    """Tests for resolve_mcp_headers."""

    def test_no_auth_returns_empty(self) -> None:
        """Test that no auth config returns empty headers."""
        spec = MCPServerSpec(name="test", url="http://mcp:8080")
        assert resolve_mcp_headers(spec) == {}

    def test_env_var_auth(self) -> None:
        """Test env_var auth resolves from environment."""
        spec = MCPServerSpec(
            name="test",
            url="http://mcp:8080",
            auth=MCPAuthSpec(type="env_var", env_var="MCP_TOKEN"),
        )
        with patch.dict("os.environ", {"MCP_TOKEN": "secret123"}):
            headers = resolve_mcp_headers(spec)
        assert headers == {"Authorization": "Bearer secret123"}

    def test_env_var_missing_raises(self) -> None:
        """Test that missing env var raises ValueError."""
        spec = MCPServerSpec(
            name="test",
            url="http://mcp:8080",
            auth=MCPAuthSpec(type="env_var", env_var="MISSING_VAR"),
        )
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="not set"):
                resolve_mcp_headers(spec)

    def test_env_var_no_field_raises(self) -> None:
        """Test that env_var type without env_var field raises."""
        spec = MCPServerSpec(
            name="test",
            url="http://mcp:8080",
            auth=MCPAuthSpec(type="env_var"),
        )
        with pytest.raises(ValueError, match="requires env_var"):
            resolve_mcp_headers(spec)

    def test_header_value_auth(self) -> None:
        """Test inline header_value auth."""
        spec = MCPServerSpec(
            name="test",
            url="http://mcp:8080",
            auth=MCPAuthSpec(type="header_value", header_value="tok123"),
        )
        headers = resolve_mcp_headers(spec)
        assert headers == {"Authorization": "Bearer tok123"}

    def test_header_value_no_field_raises(self) -> None:
        """Test that header_value type without value raises."""
        spec = MCPServerSpec(
            name="test",
            url="http://mcp:8080",
            auth=MCPAuthSpec(type="header_value"),
        )
        with pytest.raises(ValueError, match="requires header_value"):
            resolve_mcp_headers(spec)

    def test_custom_header_name_and_prefix(self) -> None:
        """Test custom header name and prefix."""
        spec = MCPServerSpec(
            name="test",
            url="http://mcp:8080",
            auth=MCPAuthSpec(
                type="env_var",
                env_var="API_KEY",
                header_name="X-API-Key",
                header_prefix="",
            ),
        )
        with patch.dict("os.environ", {"API_KEY": "mykey"}):
            headers = resolve_mcp_headers(spec)
        assert headers == {"X-API-Key": "mykey"}


class TestLoadMcpServers:
    """Tests for load_mcp_servers."""

    def test_loads_servers(self) -> None:
        """Test loading MCP server instances."""
        specs = [
            MCPServerSpec(name="server1", url="http://mcp1:8080"),
            MCPServerSpec(name="server2", url="http://mcp2:9090"),
        ]
        servers = load_mcp_servers(specs)
        assert len(servers) == 2

    def test_empty_list(self) -> None:
        """Test loading empty spec list."""
        servers = load_mcp_servers([])
        assert servers == []
