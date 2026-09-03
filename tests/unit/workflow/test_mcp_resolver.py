"""Tests for unified MCP resolver (issue #265)."""

from __future__ import annotations

import pytest

from cloud_agents.workflow.core.mcp_resolver import resolve_mcp_servers
from cloud_agents.workflow.core.models import MCPServerConfig, SecretHeaderRef


class TestResolveMCPEmptyNone:
    def test_none_returns_none(self):
        assert resolve_mcp_servers(None, [{"name": "a", "url": "http://a"}]) is None

    def test_empty_list_returns_none(self):
        assert resolve_mcp_servers([], [{"name": "a", "url": "http://a"}]) is None

    def test_none_catalog(self):
        assert resolve_mcp_servers(["a"], None) is None

    def test_empty_catalog(self):
        assert resolve_mcp_servers(["a"], []) is None


class TestResolveMCPReferenceByName:
    def test_subset_of_catalog(self):
        catalog = [
            {"name": "a", "url": "http://a"},
            {"name": "b", "url": "http://b"},
            {"name": "c", "url": "http://c"},
        ]
        assert resolve_mcp_servers(["a", "c"], catalog) == [
            {"name": "a", "url": "http://a"},
            {"name": "c", "url": "http://c"},
        ]

    def test_single_name(self):
        catalog = [{"name": "a", "url": "http://a"}]
        result = resolve_mcp_servers(["a"], catalog)
        assert result == [{"name": "a", "url": "http://a"}]

    def test_unknown_name_is_dropped(self):
        catalog = [{"name": "a", "url": "http://a"}]
        assert resolve_mcp_servers(["missing"], catalog) is None
        # Mixed known + unknown -> only known returned
        assert resolve_mcp_servers(["a", "missing"], catalog) == [
            {"name": "a", "url": "http://a"}
        ]

    def test_catalog_as_pydantic_models(self):
        catalog = [
            MCPServerConfig(name="a", url="http://a"),
            MCPServerConfig(name="b", url="http://b"),
        ]
        assert resolve_mcp_servers(["b"], catalog) == [
            {"name": "b", "url": "http://b", "headers": None, "secret_headers": None}
        ]


class TestResolveMCPInline:
    def test_inline_dict_no_catalog_needed(self):
        inline = {"name": "inline", "url": "http://inline"}
        assert resolve_mcp_servers([inline], []) == [inline]

    def test_inline_with_headers(self):
        inline = {"name": "inline", "url": "http://x", "headers": {"Auth": "tok"}}
        assert resolve_mcp_servers([inline], None) == [inline]

    def test_inline_as_pydantic_model(self):
        cfg = MCPServerConfig(name="inline", url="http://x")
        result = resolve_mcp_servers([cfg], [])
        assert result is not None
        assert result[0]["name"] == "inline"
        assert result[0]["url"] == "http://x"

    def test_inline_with_secret_headers_model(self):
        cfg = MCPServerConfig(
            name="inline",
            url="http://x",
            secret_headers={"Auth": SecretHeaderRef(secret_name="s", key="k")},
        )
        result = resolve_mcp_servers([cfg], [])
        assert result is not None
        # After model_dump, secret_headers should be plain dict
        assert result[0]["secret_headers"]["Auth"]["secret_name"] == "s"

    def test_mixed_reference_and_inline(self):
        catalog = [{"name": "a", "url": "http://a"}]
        inline = {"name": "inline", "url": "http://x"}
        result = resolve_mcp_servers(["a", inline], catalog)
        assert result == [{"name": "a", "url": "http://a"}, inline]

    def test_mixed_pydantic_inline_and_str(self):
        catalog = [MCPServerConfig(name="a", url="http://a")]
        inline = MCPServerConfig(name="inline", url="http://x")
        result = resolve_mcp_servers(["a", inline], catalog)
        assert result is not None
        names = [s["name"] for s in result]
        assert names == ["a", "inline"]


class TestResolveMCPPreservesOrder:
    def test_order_is_step_order(self):
        catalog = [
            {"name": "a", "url": "http://a"},
            {"name": "b", "url": "http://b"},
        ]
        inline = {"name": "inline", "url": "http://x"}
        result = resolve_mcp_servers([inline, "a"], catalog)
        assert result is not None
        assert [s["name"] for s in result] == ["inline", "a"]
