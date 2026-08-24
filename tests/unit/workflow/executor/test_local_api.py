"""Tests for the local workflow runner API endpoints.

Covers:
- GET /tools — list registered tools with names and descriptions
- POST /run — submit-time 422 validation for unknown tool names
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agents.workflow.executor.step.tools import clear_tools, register_tool


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear tool registry before/after each test."""
    clear_tools()
    yield
    clear_tools()


def _build_test_app(
    mock_executor: AsyncMock | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the local router for testing."""
    from cloud_agents.workflow.executor.local.api import build_local_router

    app = FastAPI()
    executor = mock_executor or AsyncMock()
    router = build_local_router(executor=executor)
    app.include_router(router, prefix="/v1/workflows")
    return app


def _dummy_func() -> str:
    """A dummy tool function for testing."""
    return "result"


# =================================================================
# GET /v1/workflows/tools — list registered tools
# =================================================================


class TestListToolsEndpoint:
    """Tests for GET /v1/workflows/tools."""

    def test_empty_registry_returns_empty_list(self) -> None:
        """Endpoint returns empty tools list when no tools are registered."""
        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/workflows/tools")
        assert response.status_code == 200
        data = response.json()
        assert data == {"tools": []}

    def test_returns_registered_tools(self) -> None:
        """Endpoint returns all registered tools with names and descriptions."""
        register_tool("alpha_tool", _dummy_func, description="Does alpha things")
        register_tool("beta_tool", _dummy_func, description="Does beta things")

        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/workflows/tools")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tools"]) == 2
        assert data["tools"][0]["name"] == "alpha_tool"
        assert data["tools"][0]["description"] == "Does alpha things"
        assert data["tools"][1]["name"] == "beta_tool"
        assert data["tools"][1]["description"] == "Does beta things"

    def test_tools_sorted_by_name(self) -> None:
        """Tools are returned sorted by name."""
        register_tool("zulu", _dummy_func, description="Z tool")
        register_tool("alpha", _dummy_func, description="A tool")
        register_tool("mike", _dummy_func, description="M tool")

        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/workflows/tools")
        names = [t["name"] for t in response.json()["tools"]]
        assert names == ["alpha", "mike", "zulu"]

    def test_each_tool_has_name_and_description_fields(self) -> None:
        """Each tool entry contains exactly name and description keys."""
        register_tool("my_tool", _dummy_func, description="A test tool")

        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/workflows/tools")
        tool = response.json()["tools"][0]
        assert set(tool.keys()) == {"name", "description"}

    def test_tool_with_no_description(self) -> None:
        """Tool registered without description returns None for description."""
        register_tool("bare_tool", _dummy_func)

        app = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/workflows/tools")
        tool = response.json()["tools"][0]
        assert tool["name"] == "bare_tool"
        assert tool["description"] is None


# =================================================================
# POST /v1/workflows/run — unknown tool validation (422)
# =================================================================


def _make_run_body(
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a minimal workflow run request body."""
    return {
        "definition": {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test-workflow"},
            "spec": {"steps": steps},
        },
        "provider": {
            "name": "openai",
            "model": "gpt-4o",
            "credentials_secret": "openai-api-key",
        },
        "sandbox_image": "sandbox:latest",
    }


class TestRunWorkflowToolValidation:
    """Tests for submit-time tool name validation on POST /run."""

    def test_valid_tools_pass_validation(self) -> None:
        """Steps referencing only registered tools pass validation."""
        register_tool("kubectl_get", _dummy_func, description="Get K8s resources")
        register_tool("http_request", _dummy_func, description="HTTP requests")

        mock_executor = AsyncMock()
        mock_executor.start.return_value = "wf-123"

        app = _build_test_app(mock_executor)
        client = TestClient(app, raise_server_exceptions=False)

        body = _make_run_body([
            {
                "name": "check",
                "type": "agent",
                "prompt": "check pods",
                "output_key": "r1",
                "tools": ["kubectl_get", "http_request"],
            },
        ])

        response = client.post("/v1/workflows/run", json=body)
        assert response.status_code == 202

    def test_unknown_tools_return_422(self) -> None:
        """Steps referencing unknown tools are rejected with 422."""
        register_tool("kubectl_get", _dummy_func, description="Get K8s resources")

        mock_executor = AsyncMock()
        app = _build_test_app(mock_executor)
        client = TestClient(app, raise_server_exceptions=False)

        body = _make_run_body([
            {
                "name": "check",
                "type": "agent",
                "prompt": "check pods",
                "output_key": "r1",
                "tools": ["kubectl_get", "nonexistent_tool"],
            },
        ])

        response = client.post("/v1/workflows/run", json=body)
        assert response.status_code == 422
        assert "nonexistent_tool" in response.json()["detail"]

    def test_steps_without_tools_pass(self) -> None:
        """Steps that don't reference any tools pass validation."""
        mock_executor = AsyncMock()
        mock_executor.start.return_value = "wf-456"

        app = _build_test_app(mock_executor)
        client = TestClient(app, raise_server_exceptions=False)

        body = _make_run_body([
            {
                "name": "summarize",
                "type": "agent",
                "prompt": "summarize the log",
                "output_key": "r1",
            },
        ])

        response = client.post("/v1/workflows/run", json=body)
        assert response.status_code == 202

    def test_empty_tools_list_passes(self) -> None:
        """Steps with an empty tools list pass validation."""
        mock_executor = AsyncMock()
        mock_executor.start.return_value = "wf-789"

        app = _build_test_app(mock_executor)
        client = TestClient(app, raise_server_exceptions=False)

        body = _make_run_body([
            {
                "name": "step1",
                "type": "agent",
                "prompt": "do something",
                "output_key": "r1",
                "tools": [],
            },
        ])

        response = client.post("/v1/workflows/run", json=body)
        assert response.status_code == 202

    def test_multiple_steps_unknown_tools(self) -> None:
        """Validation catches unknown tools in any step, not just the first."""
        register_tool("http_request", _dummy_func, description="HTTP requests")

        mock_executor = AsyncMock()
        app = _build_test_app(mock_executor)
        client = TestClient(app, raise_server_exceptions=False)

        body = _make_run_body([
            {
                "name": "step1",
                "type": "agent",
                "prompt": "first step",
                "output_key": "r1",
                "tools": ["http_request"],
            },
            {
                "name": "step2",
                "type": "agent",
                "prompt": "second step",
                "output_key": "r2",
                "tools": ["missing_tool"],
            },
        ])

        response = client.post("/v1/workflows/run", json=body)
        assert response.status_code == 422
        assert "missing_tool" in response.json()["detail"]
        assert "step2" in response.json()["detail"]

    def test_422_detail_includes_registered_tools(self) -> None:
        """422 error message includes list of registered tools."""
        register_tool("kubectl_get", _dummy_func, description="Get K8s resources")

        mock_executor = AsyncMock()
        app = _build_test_app(mock_executor)
        client = TestClient(app, raise_server_exceptions=False)

        body = _make_run_body([
            {
                "name": "check",
                "type": "agent",
                "prompt": "check",
                "output_key": "r1",
                "tools": ["bad_tool"],
            },
        ])

        response = client.post("/v1/workflows/run", json=body)
        detail = response.json()["detail"]
        assert "kubectl_get" in detail
