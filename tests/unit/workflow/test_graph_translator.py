"""Tests for the YAML → pydantic-graph translation layer."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture


def _make_definition(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal workflow definition dict."""
    return {
        "apiVersion": "v1",
        "kind": "AgentWorkflow",
        "metadata": {"name": "test-workflow"},
        "spec": {"steps": steps},
    }


class TestGraphTranslator:
    """Tests for translating workflow YAML to pydantic-graph."""

    def test_single_agent_step(self) -> None:
        """Single agent step produces a graph with start → agent → end."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None
        assert state is not None

    def test_two_sequential_steps(self) -> None:
        """Two agent steps produce start → A → B → end."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Diagnose",
                "output_key": "diagnosis",
            },
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix it",
                "output_key": "fix_result",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None

    def test_approval_step_included(self) -> None:
        """Human-approval step translates to a graph node."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Diagnose",
                "output_key": "diagnosis",
            },
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve the fix?",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None

    def test_state_has_workflow_id(self) -> None:
        """Workflow state carries the workflow_id."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        _, state = build_graph(defn, workflow_id="wf-test-42")
        assert state.workflow_id == "wf-test-42"

    def test_state_has_step_definitions(self) -> None:
        """Workflow state carries the step definitions for runtime access."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check it",
                "output_key": "diagnosis",
            },
        ])

        _, state = build_graph(defn, workflow_id="wf-1")
        assert "diagnose" in state.step_defs

    @pytest.mark.asyncio
    async def test_agent_step_calls_step_runner(
        self, mocker: MockerFixture
    ) -> None:
        """Agent step node calls step_runner.run_step during execution."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "all good"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            sandbox_image="sandbox:latest",
        )

        result = await graph.run(state=state)
        mock_executor.run.assert_called_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_auto_approve_completes(self, mocker: MockerFixture) -> None:
        """Approval step with auto_approve=True completes without pausing."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve?",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            approval_policy={"auto_approve": True},
        )

        result = await graph.run(state=state)
        assert result["status"] == "completed"
        assert state.step_results["approval"]["output"]["auto_approved"] is True
        assert state.paused_at_step is None

    @pytest.mark.asyncio
    async def test_approval_signals_pause(self, mocker: MockerFixture) -> None:
        """Approval step without auto_approve signals pause."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve?",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")

        result = await graph.run(state=state)
        assert state.paused_at_step == "approve"
        assert state.step_results["approval"]["status"] == "awaiting_approval"

    @pytest.mark.asyncio
    async def test_step_results_keyed_by_output_key(
        self, mocker: MockerFixture
    ) -> None:
        """Agent step stores results under output_key, not step name."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"found": "bug"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check",
                "output_key": "my_diagnosis",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert "my_diagnosis" in state.step_results
        assert state.step_results["my_diagnosis"]["output"]["found"] == "bug"

    @pytest.mark.asyncio
    async def test_condition_false_skips_step(self, mocker: MockerFixture) -> None:
        """Step with false condition is skipped."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.evaluate_condition",
            return_value=False,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
                "condition": "steps.diag.output.severity == 'high'",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        mock_executor.run.assert_not_called()
        assert state.step_results["r1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_condition_true_runs_step(self, mocker: MockerFixture) -> None:
        """Step with true condition runs normally."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.evaluate_condition",
            return_value=True,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
                "condition": "steps.diag.output.severity == 'high'",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.step_results["r1"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_pause_guard_skips_subsequent_steps(
        self, mocker: MockerFixture
    ) -> None:
        """Steps after approval gate are skipped when workflow is paused."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve?",
            },
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix it",
                "output_key": "fix_result",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.paused_at_step == "approve"
        mock_executor.run.assert_not_called()
        assert state.step_results["fix_result"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_condition_integration_unmocked(
        self, mocker: MockerFixture
    ) -> None:
        """Condition evaluation works end-to-end without mocking evaluate_condition."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"severity": "low"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Diagnose",
                "output_key": "diagnosis",
            },
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix",
                "output_key": "fix_result",
                "condition": "steps.diagnosis.output.severity == 'high'",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.step_results["fix_result"]["status"] == "skipped"

    def test_parallel_group_warns(self, caplog: Any) -> None:
        """Step with parallel_group logs a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            from cloud_agents.workflow.executor.graph_translator import build_graph

            defn = _make_definition([
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                    "parallel_group": "group-a",
                },
            ])

            build_graph(defn, workflow_id="wf-1")

        assert any("parallel_group" in r.message for r in caplog.records)

    def test_no_temporal_imports(self) -> None:
        """graph_translator module has zero temporalio imports."""
        from cloud_agents.workflow.executor import graph_translator as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source
