"""Unit tests for workflow definition API endpoints."""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.workflow.api import create_workflow_app
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.executor import WorkflowExecutor


def _make_defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "placeholder"},
        spec=WorkflowSpec(steps=[
            WorkflowStepSpec(name="s1", type="agent", agent="diag",
                             prompt="test", output_key="r1", spawn="pre-deployed"),
        ]),
    )


def _make_executor() -> WorkflowExecutor:
    registry = AgentRegistry({"diag": "http://diag:8080"})
    client = AsyncMock()
    client.run = AsyncMock(return_value=AgentRunResponse(
        output={"ok": True}, output_type="str",
        usage={"input_tokens": 1, "output_tokens": 1},
        agent_name="diag", success=True,
    ))
    return WorkflowExecutor(_make_defn(), registry, client_factory=lambda _: client)


SUBMIT_YAML = """
apiVersion: v1
kind: AgentWorkflow
metadata:
  name: test-submitted
spec:
  steps:
    - name: check
      type: agent
      agent: diag
      prompt: check hosts
      output_key: result
      spawn: pre-deployed
"""


class TestDefinitionSubmit:
    """Tests for POST /v1/workflows/definitions."""

    @pytest.mark.asyncio
    async def test_submit_valid_definition(self) -> None:
        """Test submitting a valid workflow definition."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/workflows/definitions",
                content=SUBMIT_YAML.encode(),
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "test-submitted"
        assert body["version"] == 1

    @pytest.mark.asyncio
    async def test_submit_invalid_yaml(self) -> None:
        """Test submitting invalid YAML."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/workflows/definitions",
                content=b"not: valid: [yaml",
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_submit_missing_fields(self) -> None:
        """Test submitting YAML with missing required fields."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/workflows/definitions",
                content=b"apiVersion: v1\nkind: AgentWorkflow",
            )
        assert resp.status_code == 422


class TestDefinitionList:
    """Tests for GET /v1/workflows/definitions."""

    @pytest.mark.asyncio
    async def test_list_empty(self) -> None:
        """Test listing with no definitions."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/v1/workflows/definitions")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_after_submit(self) -> None:
        """Test listing after submitting a definition."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/v1/workflows/definitions", content=SUBMIT_YAML.encode())
            resp = await client.get("/v1/workflows/definitions")
        assert resp.status_code == 200
        defs = resp.json()
        assert len(defs) == 1
        assert defs[0]["name"] == "test-submitted"


class TestDefinitionGet:
    """Tests for GET /v1/workflows/definitions/{name}."""

    @pytest.mark.asyncio
    async def test_get_existing(self) -> None:
        """Test getting a submitted definition."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/v1/workflows/definitions", content=SUBMIT_YAML.encode())
            resp = await client.get("/v1/workflows/definitions/test-submitted")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "test-submitted"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self) -> None:
        """Test getting a nonexistent definition."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/v1/workflows/definitions/missing")
        assert resp.status_code == 404


class TestDefinitionDelete:
    """Tests for DELETE /v1/workflows/definitions/{name}."""

    @pytest.mark.asyncio
    async def test_delete_existing(self) -> None:
        """Test deleting a submitted definition."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/v1/workflows/definitions", content=SUBMIT_YAML.encode())
            resp = await client.delete("/v1/workflows/definitions/test-submitted")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self) -> None:
        """Test deleting a nonexistent definition."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/v1/workflows/definitions/missing")
        assert resp.status_code == 404


class TestRunByName:
    """Tests for POST /v1/workflows/run with workflow_name."""

    @pytest.mark.asyncio
    async def test_run_by_name(self) -> None:
        """Test running a submitted workflow by name."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/v1/workflows/definitions", content=SUBMIT_YAML.encode())
            resp = await client.post(
                "/v1/workflows/run",
                json={"workflow_name": "test-submitted"},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] in ("completed", "failed", "paused")

    @pytest.mark.asyncio
    async def test_run_by_name_not_found(self) -> None:
        """Test running a nonexistent workflow name."""
        app = create_workflow_app(_make_executor(), "test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/workflows/run",
                json={"workflow_name": "missing-wf"},
            )
        assert resp.status_code == 404
