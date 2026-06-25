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
    step_specs = [WorkflowStepSpec(**{**s, "spawn": s.get("spawn", "pre-deployed")}) for s in steps]
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


class TestWorkflowAPIAuth:
    """Tests for workflow API authentication via BearerAuthMiddleware."""

    def test_workflow_endpoints_require_token_when_set(self) -> None:
        """Test that all workflow endpoints are protected when AGENT_API_TOKEN is set."""
        import os
        from unittest.mock import patch

        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diag",
             "prompt": "test", "output_key": "r1"},
        ])
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry)

        with patch.dict(os.environ, {"AGENT_API_TOKEN": "secret-123"}):
            app = create_workflow_app(executor, "test")
        tc = TestClient(app)

        assert tc.post("/v1/workflows/run", json={}).status_code == 401
        assert tc.get("/v1/workflows").status_code == 401
        assert tc.get("/v1/workflows/abc").status_code == 401
        assert tc.post("/v1/workflows/abc/approve", json={}).status_code == 401

    def test_healthz_exempt_from_auth(self) -> None:
        """Test that /healthz is accessible without a token."""
        import os
        from unittest.mock import patch

        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diag",
             "prompt": "test", "output_key": "r1"},
        ])
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry)

        with patch.dict(os.environ, {"AGENT_API_TOKEN": "secret-123"}):
            app = create_workflow_app(executor, "test")
        tc = TestClient(app)

        assert tc.get("/healthz").status_code == 200


class TestWorkflowAPIIngest:
    """Tests for POST /v1/workflows/{id}/steps/{step}/result."""

    def _make_app_with_persistence(self):
        """Create app with in-memory persistence."""
        from agents.workflow.persistence import InMemoryPersistence
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diag",
             "prompt": "Do something", "output_key": "result"},
        ])
        registry = AgentRegistry({"diag": "http://diag:8080"})
        persistence = InMemoryPersistence()
        executor = WorkflowExecutor(defn, registry, persistence=persistence)
        app = create_workflow_app(executor, "test-workflow")
        return TestClient(app), persistence

    def test_ingest_valid_result(self) -> None:
        """Valid result callback returns 200."""
        from agents.workflow.state import StepResult, WorkflowState
        tc, persistence = self._make_app_with_persistence()

        import asyncio
        state = WorkflowState(
            workflow_id="wf-1", workflow_name="test", status="running",
            steps={"result": StepResult(
                step_name="step1", status="dispatched",
                started_at="2026-01-01T00:00:00Z",
                output={"spawned_name": "agent-abc", "attempt": 1},
            )},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        loop = asyncio.new_event_loop()
        loop.run_until_complete(persistence.save(state))
        loop.close()

        resp = tc.post("/v1/workflows/wf-1/steps/result/result", json={
            "status": "completed",
            "output": {"summary": "done"},
            "completed_at": "2026-01-01T00:01:00Z",
            "attempt": 1,
        })
        assert resp.status_code == 200
        assert resp.json()["workflow_id"] == "wf-1"

    def test_ingest_unknown_workflow_404(self) -> None:
        """Unknown workflow returns 404."""
        tc, _ = self._make_app_with_persistence()
        resp = tc.post("/v1/workflows/nonexistent/steps/result/result", json={
            "status": "completed",
            "output": {},
            "completed_at": "2026-01-01T00:01:00Z",
            "attempt": 1,
        })
        assert resp.status_code == 404

    def test_ingest_invalid_payload_422(self) -> None:
        """Invalid payload returns 422."""
        tc, _ = self._make_app_with_persistence()
        resp = tc.post("/v1/workflows/wf-1/steps/result/result", json={
            "bad_field": "value",
        })
        assert resp.status_code == 422

    def test_ingest_auth_required(self) -> None:
        """Ingest endpoint requires auth when token is set."""
        import os
        from unittest.mock import patch

        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diag",
             "prompt": "test", "output_key": "r1"},
        ])
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry)
        with patch.dict(os.environ, {"AGENT_API_TOKEN": "secret-123"}):
            app = create_workflow_app(executor, "test")
        tc = TestClient(app)

        resp = tc.post("/v1/workflows/wf-1/steps/r1/result", json={
            "status": "completed", "output": {},
            "completed_at": "2026-01-01T00:01:00Z", "attempt": 1,
        })
        assert resp.status_code == 401


class TestWorkflowAPIList:
    """Tests for /v1/workflows."""

    def test_list_empty(self) -> None:
        """Test listing when no workflows exist."""
        tc = _make_app_with_agent_step()
        resp = tc.get("/v1/workflows")
        assert resp.status_code == 200
        assert resp.json() == []
