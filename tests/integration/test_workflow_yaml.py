"""Integration test: full workflow YAML end-to-end execution.

Loads actual workflow YAML files from examples/workflow-definitions/,
builds the pydantic-graph, and runs them through the real dispatch pipeline.

The LLM is mocked (it is an external service), but everything else is real:
- YAML parsing and validation
- build_graph graph construction
- Step dispatch (get_step_executor selection)
- StepInput construction with context threading
- Condition evaluation
- Approval gate auto-approve logic

This catches wiring breaks that unit tests with mocked dispatch cannot.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from pytest_mock import MockerFixture

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "workflow-definitions"


def _mock_llm_response(mocker: MockerFixture, text: str) -> AsyncMock:
    """Create a mock model_request that returns a fixed text response.

    Parameters:
        mocker: pytest-mock fixture.
        text: Response text to return.

    Returns:
        The mocked model_request function.
    """
    mock_response = mocker.MagicMock()
    mock_response.text = text
    mock_response.output = text
    mock_usage = mocker.MagicMock()
    mock_usage.input_tokens = 50
    mock_usage.output_tokens = 20
    mock_response.usage = mock_usage

    return mock_response


class TestTriageClassifyWorkflow:
    """End-to-end test using the real triage-classify-workflow.yaml."""

    @pytest.mark.asyncio
    async def test_triage_classify_workflow_completes(self, mocker: MockerFixture) -> None:
        """Load triage-classify-workflow.yaml and run through the full pipeline.

        All agent steps use mocked LLM, but the full dispatch pipeline is real:
        build_graph -> get_step_executor -> DirectExecutor -> model_request.
        """
        yaml_path = _EXAMPLES_DIR / "triage-classify-workflow.yaml"
        assert yaml_path.exists(), f"Expected workflow YAML at {yaml_path}"

        with open(yaml_path) as f:
            defn = yaml.safe_load(f)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        # Mock model_request (LLM is external, but dispatch is real)
        triage_response = '{"severity": "high", "category": "resource", "summary": "OOM detected"}'
        runbook_response = '{"title": "OOM Fix", "steps": [{"action": "check memory", "command": "kubectl top pods"}]}'

        call_count = 0

        async def fake_model_request(model, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_llm_response(mocker, triage_response)
            return _mock_llm_response(mocker, runbook_response)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            side_effect=fake_model_request,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        graph, state = build_graph(
            defn,
            workflow_id="wf-yaml-e2e-1",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            approval_policy={"auto_approve": True},
        )

        from pydantic_graph import EndMarker

        async with graph.iter(state=state) as run:
            while True:
                result = await run.next()
                if isinstance(result, EndMarker):
                    break

        # Verify all steps completed or were auto-approved
        for key, step_result in state.step_results.items():
            assert step_result["status"] in (
                "completed",
                "skipped",
            ), f"Step '{key}' has unexpected status: {step_result}"

        # Verify triage output was properly structured
        assert "triage_result" in state.step_results
        triage_output = state.step_results["triage_result"]["output"]
        assert triage_output["severity"] == "high"
        assert triage_output["category"] == "resource"

        # Verify approval was auto-approved
        assert "approval" in state.step_results
        assert state.step_results["approval"]["output"]["auto_approved"] is True

        # Verify runbook step received context from triage
        assert "runbook" in state.step_results
        assert state.step_results["runbook"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_triage_classify_pauses_without_auto_approve(self, mocker: MockerFixture) -> None:
        """Without auto_approve, workflow pauses at approval gate."""
        yaml_path = _EXAMPLES_DIR / "triage-classify-workflow.yaml"
        with open(yaml_path) as f:
            defn = yaml.safe_load(f)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        triage_response = '{"severity": "low", "category": "network", "summary": "latency spike"}'

        async def fake_model_request(model, messages, **kwargs):
            return _mock_llm_response(mocker, triage_response)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            side_effect=fake_model_request,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        graph, state = build_graph(
            defn,
            workflow_id="wf-yaml-e2e-2",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            # No approval_policy -> defaults to no auto_approve
        )

        from pydantic_graph import EndMarker

        async with graph.iter(state=state) as run:
            while True:
                result = await run.next()
                if isinstance(result, EndMarker):
                    break
                # Check if paused
                if state.paused_at_step:
                    break

        # Should have paused at the approval step
        assert state.paused_at_step == "approve-escalation"
        # Triage should be completed
        assert state.step_results["triage_result"]["status"] == "completed"
        # Runbook should NOT have been reached
        assert state.step_results.get("runbook", {}).get("status") in (None, "skipped")


class TestLocalInvestigateWorkflow:
    """End-to-end test using the real local-investigate-workflow.yaml."""

    @pytest.mark.asyncio
    async def test_local_investigate_workflow_loads_and_builds(self, mocker: MockerFixture) -> None:
        """Verify local-investigate-workflow.yaml can be loaded and graph built.

        This workflow uses spawn: local (subprocess), so full execution would
        require a real LLM. We test that the YAML is valid and graph_translator
        can build the graph without errors.
        """
        yaml_path = _EXAMPLES_DIR / "local-investigate-workflow.yaml"
        assert yaml_path.exists(), f"Expected workflow YAML at {yaml_path}"

        with open(yaml_path) as f:
            defn = yaml.safe_load(f)

        from cloud_agents.workflow.executor.graph_translator import build_graph

        graph, state = build_graph(
            defn,
            workflow_id="wf-local-inv-1",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            approval_policy={"auto_approve": True},
        )

        # Verify all steps from the YAML are in the graph state
        assert "triage" in state.step_defs
        assert "investigate" in state.step_defs
        assert "approve-fix" in state.step_defs
        assert "generate-fix" in state.step_defs

        # Verify spawn modes are preserved
        assert state.step_defs["triage"]["spawn"] == "none"
        assert state.step_defs["investigate"]["spawn"] == "local"
        assert state.step_defs["generate-fix"]["spawn"] == "local"


class TestAllExampleWorkflowsLoadable:
    """Verify every YAML in examples/workflow-definitions/ loads and builds."""

    @pytest.mark.asyncio
    async def test_all_example_yamls_build_graph(self) -> None:
        """Every workflow YAML in the examples directory builds a valid graph.

        This is a regression guard: if someone changes the schema or
        graph_translator contract, we catch it immediately rather than
        discovering at deploy time.
        """
        from cloud_agents.workflow.executor.graph_translator import build_graph

        yaml_files = list(_EXAMPLES_DIR.glob("*.yaml"))
        assert len(yaml_files) > 0, "No workflow YAML files found in examples/"

        for yaml_path in yaml_files:
            with open(yaml_path) as f:
                defn = yaml.safe_load(f)

            graph, state = build_graph(
                defn,
                workflow_id=f"wf-load-test-{yaml_path.stem}",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "openai-api-key",
                },
                approval_policy={"auto_approve": True},
            )

            # Verify the graph built successfully
            spec = defn.get("spec", {})
            expected_steps = [s["name"] for s in spec.get("steps", [])]
            for step_name in expected_steps:
                assert (
                    step_name in state.step_defs
                ), f"Step '{step_name}' from {yaml_path.name} missing in graph state"
