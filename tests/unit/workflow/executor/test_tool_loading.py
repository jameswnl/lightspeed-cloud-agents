"""Tests for tool loading gaps — issue #156.

Covers:
- Temporal entrypoint calls load_builtin_tools() at startup
- Temporal entrypoint loads CLOUD_AGENTS_TOOLS_MODULE when set
- Local entrypoint loads CLOUD_AGENTS_TOOLS_MODULE after builtins
- load_tools_module() always loads builtins before the product module
- list_tool_definitions() returns name+description dicts
- GET /tools uses list_tool_definitions() instead of _REGISTRY access
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from cloud_agents.workflow.executor.step.tools import clear_tools, register_tool


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear tool registry and builtins flag before/after each test."""
    clear_tools()
    import cloud_agents.tools

    cloud_agents.tools._BUILTINS_LOADED = False
    yield
    clear_tools()
    cloud_agents.tools._BUILTINS_LOADED = False


# =================================================================
# Fix 1: Temporal entrypoint loads builtins + CLOUD_AGENTS_TOOLS_MODULE
# =================================================================


class TestTemporalEntrypointToolLoading:
    """Tests for tool loading in the Temporal entrypoint."""

    def test_temporal_entrypoint_calls_load_builtin_tools(
        self, mocker: MockerFixture,
    ) -> None:
        """build_temporal_app calls load_builtin_tools at startup."""
        mock_load = mocker.patch("cloud_agents.tools.load_builtin_tools")

        from cloud_agents.workflow.executor.temporal.entrypoint import (
            build_temporal_app,
        )

        build_temporal_app(temporal_url="localhost:7233")
        mock_load.assert_called()

    def test_temporal_entrypoint_loads_tools_module_from_env(
        self, mocker: MockerFixture, monkeypatch,
    ) -> None:
        """build_temporal_app loads CLOUD_AGENTS_TOOLS_MODULE when set."""
        monkeypatch.setenv("CLOUD_AGENTS_TOOLS_MODULE", "myapp.tools")
        mocker.patch("cloud_agents.tools.load_builtin_tools")
        mock_load_mod = mocker.patch(
            "cloud_agents.workflow.executor.step.tools.load_tools_module",
        )

        from cloud_agents.workflow.executor.temporal.entrypoint import (
            build_temporal_app,
        )

        build_temporal_app(temporal_url="localhost:7233")
        mock_load_mod.assert_called_once_with("myapp.tools")

    def test_temporal_entrypoint_skips_tools_module_when_unset(
        self, mocker: MockerFixture, monkeypatch,
    ) -> None:
        """build_temporal_app skips CLOUD_AGENTS_TOOLS_MODULE when unset."""
        monkeypatch.delenv("CLOUD_AGENTS_TOOLS_MODULE", raising=False)
        mocker.patch("cloud_agents.tools.load_builtin_tools")
        mock_load_mod = mocker.patch(
            "cloud_agents.workflow.executor.step.tools.load_tools_module",
        )

        from cloud_agents.workflow.executor.temporal.entrypoint import (
            build_temporal_app,
        )

        build_temporal_app(temporal_url="localhost:7233")
        mock_load_mod.assert_not_called()


# =================================================================
# Fix 2: Local entrypoint loads CLOUD_AGENTS_TOOLS_MODULE
# =================================================================


class TestLocalEntrypointToolLoading:
    """Tests for CLOUD_AGENTS_TOOLS_MODULE in local entrypoint."""

    def test_local_entrypoint_loads_tools_module_from_env(
        self, mocker: MockerFixture, monkeypatch,
    ) -> None:
        """build_local_app loads CLOUD_AGENTS_TOOLS_MODULE when set."""
        monkeypatch.setenv("CLOUD_AGENTS_TOOLS_MODULE", "myapp.tools")
        mocker.patch("cloud_agents.tools.load_builtin_tools")
        mock_load_mod = mocker.patch(
            "cloud_agents.workflow.executor.step.tools.load_tools_module",
        )

        from cloud_agents.workflow.executor.local.entrypoint import build_local_app

        build_local_app()
        # May be called from both temporal and local entrypoints (local
        # imports from temporal). Verify at least one call with the module.
        mock_load_mod.assert_any_call("myapp.tools")

    def test_local_entrypoint_skips_tools_module_when_unset(
        self, mocker: MockerFixture, monkeypatch,
    ) -> None:
        """build_local_app skips CLOUD_AGENTS_TOOLS_MODULE when unset."""
        monkeypatch.delenv("CLOUD_AGENTS_TOOLS_MODULE", raising=False)
        mocker.patch("cloud_agents.tools.load_builtin_tools")
        mock_load_mod = mocker.patch(
            "cloud_agents.workflow.executor.step.tools.load_tools_module",
        )

        from cloud_agents.workflow.executor.local.entrypoint import build_local_app

        build_local_app()
        mock_load_mod.assert_not_called()


# =================================================================
# Fix 3: load_tools_module always loads builtins first
# =================================================================


class TestLoadToolsModuleBuiltins:
    """Tests that load_tools_module loads builtins before the product module."""

    def test_load_tools_module_calls_builtins_first(
        self, mocker: MockerFixture,
    ) -> None:
        """load_tools_module calls load_builtin_tools before importing module."""
        call_order: list[str] = []

        mock_load_builtins = mocker.patch(
            "cloud_agents.tools.load_builtin_tools",
            side_effect=lambda: call_order.append("builtins"),
        )

        # Create a fake module in sys.modules
        import types

        fake_mod = types.ModuleType("myapp.tools")
        sys.modules["myapp.tools"] = fake_mod

        def track_import(path: str):
            call_order.append(f"import:{path}")
            return fake_mod

        mocker.patch("importlib.import_module", side_effect=track_import)

        from cloud_agents.workflow.executor.step.tools import load_tools_module

        load_tools_module("myapp.tools")

        mock_load_builtins.assert_called_once()
        assert call_order == ["builtins", "import:myapp.tools"]

        # Clean up
        sys.modules.pop("myapp.tools", None)

    def test_load_tools_module_no_longer_checks_hasattr(
        self, mocker: MockerFixture,
    ) -> None:
        """load_tools_module doesn't rely on module having load_builtin_tools."""
        mock_load_builtins = mocker.patch(
            "cloud_agents.tools.load_builtin_tools",
        )

        import types

        fake_mod = types.ModuleType("custom_product.tools")
        # Module has NO load_builtin_tools attribute
        sys.modules["custom_product.tools"] = fake_mod

        mocker.patch("importlib.import_module", return_value=fake_mod)

        from cloud_agents.workflow.executor.step.tools import load_tools_module

        load_tools_module("custom_product.tools")

        # Builtins still loaded even though module doesn't define them
        mock_load_builtins.assert_called_once()

        sys.modules.pop("custom_product.tools", None)


# =================================================================
# Fix 4: list_tool_definitions() public API
# =================================================================


class TestListToolDefinitions:
    """Tests for list_tool_definitions() public API."""

    def test_returns_empty_list_when_no_tools(self) -> None:
        """list_tool_definitions() returns empty list with no registrations."""
        from cloud_agents.workflow.executor.step.tools import list_tool_definitions

        assert list_tool_definitions() == []

    def test_returns_name_and_description(self) -> None:
        """list_tool_definitions() returns dicts with name and description."""
        register_tool("my_tool", lambda: None, description="Does things")

        from cloud_agents.workflow.executor.step.tools import list_tool_definitions

        result = list_tool_definitions()
        assert len(result) == 1
        assert result[0] == {"name": "my_tool", "description": "Does things"}

    def test_sorted_by_name(self) -> None:
        """list_tool_definitions() returns tools sorted by name."""
        register_tool("z_tool", lambda: None, description="Z")
        register_tool("a_tool", lambda: None, description="A")
        register_tool("m_tool", lambda: None, description="M")

        from cloud_agents.workflow.executor.step.tools import list_tool_definitions

        names = [t["name"] for t in list_tool_definitions()]
        assert names == ["a_tool", "m_tool", "z_tool"]

    def test_none_description_preserved(self) -> None:
        """list_tool_definitions() preserves None description."""
        register_tool("bare_tool", lambda: None)

        from cloud_agents.workflow.executor.step.tools import list_tool_definitions

        result = list_tool_definitions()
        assert result[0]["description"] is None


# =================================================================
# Fix 4b: GET /tools uses list_tool_definitions
# =================================================================


class TestGetToolsEndpointUsesList:
    """Tests that GET /tools uses list_tool_definitions instead of _REGISTRY."""

    def test_endpoint_calls_list_tool_definitions(
        self, mocker: MockerFixture,
    ) -> None:
        """GET /tools delegates to list_tool_definitions()."""
        from cloud_agents.workflow.executor.local.api import build_local_router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        # Patch at the source module since api.py uses a local import
        mock_list = mocker.patch(
            "cloud_agents.workflow.executor.step.tools.list_tool_definitions",
            return_value=[{"name": "test_tool", "description": "A test"}],
        )

        app = FastAPI()
        router = build_local_router(executor=AsyncMock())
        app.include_router(router, prefix="/v1/workflows")

        client = TestClient(app)
        response = client.get("/v1/workflows/tools")

        assert response.status_code == 200
        mock_list.assert_called_once()
        assert response.json() == {
            "tools": [{"name": "test_tool", "description": "A test"}],
        }
