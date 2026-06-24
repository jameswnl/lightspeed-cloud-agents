"""Unit tests for graph state and dependency models."""

from __future__ import annotations

from agents.registry import AgentRegistry
from agents.workflow.graph_state import GraphWorkflowDeps, GraphWorkflowState
from agents.workflow.state import StepResult, WorkflowState


class TestGraphWorkflowState:
    """Tests for GraphWorkflowState."""

    def test_wraps_workflow_state(self) -> None:
        """Test that GraphWorkflowState wraps WorkflowState."""
        ws = WorkflowState(
            workflow_id="wf-1", workflow_name="test",
            created_at="2026-01-01", updated_at="2026-01-01",
        )
        gws = GraphWorkflowState(workflow_state=ws)
        assert gws.workflow_state.workflow_id == "wf-1"

    def test_step_results_accumulate(self) -> None:
        """Test that step results can be accumulated in the wrapped state."""
        ws = WorkflowState(
            workflow_id="wf-1", workflow_name="test",
            created_at="2026-01-01", updated_at="2026-01-01",
        )
        gws = GraphWorkflowState(workflow_state=ws)
        gws.workflow_state.steps["s1"] = StepResult(
            step_name="s1", status="completed", output={"result": "ok"},
        )
        assert gws.workflow_state.steps["s1"].output["result"] == "ok"


class TestGraphWorkflowDeps:
    """Tests for GraphWorkflowDeps."""

    def test_minimal_deps(self) -> None:
        """Test creating deps with minimal required fields."""
        registry = AgentRegistry({"agent-1": "http://agent:8080"})
        deps = GraphWorkflowDeps(
            registry=registry,
            client_factory=lambda name: None,
        )
        assert deps.registry.get_endpoint("agent-1") == "http://agent:8080"
        assert deps.spawner is None
        assert deps.agent_image == "agent-runtime:latest"

    def test_full_deps(self) -> None:
        """Test creating deps with all fields."""
        from unittest.mock import AsyncMock
        deps = GraphWorkflowDeps(
            registry=AgentRegistry({}),
            client_factory=lambda name: None,
            spawner=AsyncMock(),
            agent_image="custom:v1",
        )
        assert deps.agent_image == "custom:v1"
        assert deps.spawner is not None
