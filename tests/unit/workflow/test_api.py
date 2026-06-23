"""Unit tests for Workflow API."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.workflow.api import create_workflow_app
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.executor import WorkflowExecutor


def _make_definition(steps: list[dict]) -> WorkflowDefinition:
    step_specs = [WorkflowStepSpec(**s) for s in steps]
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "test-workflow"},
        spec=WorkflowSpec(steps=step_specs),
    )


def _make_response(output: dict) -> AgentRunResponse:
    return AgentRunResponse(
        output=output, output_type="test",
        usage={"input_tokens": 1, "output_tokens": 2},
        agent_name="test", success=True,
    )


def _make_app_with_agent_step() -> TestClient:
    defn = _make_definition([
        {"name": "step1", "type": "agent", "agent": "diag",
         "prompt": "Do something", "output_key": "result"},
    ])
    client = AsyncMock()
    client.run = AsyncMock(return_value=_make_response({"summary": "done"}))
    registry = AgentRegistry({"diag": "http://diag:8080"})
    executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)
    app = create_workflow_app(executor, "test-workflow")
    return TestClient(app)


def _make_app_with_approval() -> tuple[TestClient, WorkflowExecutor]:
    defn = _make_definition([
        {"name": "approve", "type": "human-approval",
         "message": "OK?", "output_key": "approval"},
    ])
    registry = AgentRegistry({})
    executor = WorkflowExecutor(defn, registry)
    app = create_workflow_app(executor, "test-workflow")
    return TestClient(app), executor


class TestWorkflowAPIHealthz:
    """Tests for /healthz."""

    def test_healthz(self) -> None:
        """Test healthz returns ready."""
        tc = _make_app_with_agent_step()
        resp = tc.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["workflow"] == "test-workflow"


class TestWorkflowAPIRun:
    """Tests for /v1/workflows/run."""

    def test_run_returns_202(self) -> None:
        """Test that starting a workflow returns 202."""
        tc = _make_app_with_agent_step()
        resp = tc.post("/v1/workflows/run", json={})
        assert resp.status_code == 202
        assert "workflow_id" in resp.json()


class TestWorkflowAPIPoll:
    """Tests for /v1/workflows/{id}."""

    def test_unknown_workflow_returns_404(self) -> None:
        """Test polling a nonexistent workflow."""
        tc = _make_app_with_agent_step()
        resp = tc.get("/v1/workflows/nonexistent")
        assert resp.status_code == 404


class TestWorkflowAPIApprove:
    """Tests for /v1/workflows/{id}/approve."""

    def test_approve_paused_workflow(self) -> None:
        """Test approving a paused workflow."""
        import time
        tc, executor = _make_app_with_approval()

        run_resp = tc.post("/v1/workflows/run", json={})
        assert run_resp.status_code == 202
        workflow_id = run_resp.json()["workflow_id"]

        time.sleep(0.2)

        approve_resp = tc.post(
            f"/v1/workflows/{workflow_id}/approve",
            json={"approved": True},
        )
        assert approve_resp.status_code == 200
        body = approve_resp.json()
        assert body["steps"]["approval"]["output"]["approved"] is True


class TestWorkflowAPIApprovalAuth:
    """Tests for approval endpoint authentication."""

    def test_invalid_token_rejected(self) -> None:
        """Test that an invalid token is rejected."""
        import agents.workflow.api as api_module
        original = api_module.APPROVAL_TOKEN
        api_module.APPROVAL_TOKEN = "secret-token"
        try:
            tc, executor = _make_app_with_approval()
            import time
            tc.post("/v1/workflows/run", json={})
            time.sleep(0.1)
            workflows = tc.get("/v1/workflows").json()
            if workflows:
                wid = workflows[0]["workflow_id"]
                resp = tc.post(
                    f"/v1/workflows/{wid}/approve",
                    json={"approved": True},
                    headers={"Authorization": "Bearer wrong-token"},
                )
                assert resp.status_code == 401
        finally:
            api_module.APPROVAL_TOKEN = original


class TestWorkflowAPIList:
    """Tests for /v1/workflows."""

    def test_list_empty(self) -> None:
        """Test listing when no workflows exist."""
        tc = _make_app_with_agent_step()
        resp = tc.get("/v1/workflows")
        assert resp.status_code == 200
        assert resp.json() == []
