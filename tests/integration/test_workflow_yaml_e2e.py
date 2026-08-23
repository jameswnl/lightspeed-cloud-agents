"""Integration: workflow YAML end-to-end through graph translator."""

from __future__ import annotations

import glob
import os
from unittest.mock import AsyncMock

import pytest
import yaml
from pytest_mock import MockerFixture


class TestWorkflowYamlE2E:
    """Run real YAML definitions through the full graph pipeline with mocked LLM."""

    @pytest.mark.asyncio
    async def test_triage_classify_completes(self, mocker: MockerFixture) -> None:
        """triage-classify-workflow.yaml runs through full pipeline."""
        with open("examples/workflow-definitions/triage-classify-workflow.yaml") as f:
            defn = yaml.safe_load(f)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})

        # Two agent steps need LLM responses: triage and generate-runbook
        triage_response = mocker.MagicMock()
        triage_response.text = (
            '{"severity": "high", "category": "resource",'
            ' "summary": "OOM", "recommended_action": "restart"}'
        )
        triage_response.usage = mocker.MagicMock(input_tokens=50, output_tokens=20)

        runbook_response = mocker.MagicMock()
        runbook_response.text = (
            '{"title": "OOM Runbook", "steps": [{"action": "check pods",'
            ' "command": "kubectl get pods", "expected_output": "pod list"}],'
            ' "estimated_time_minutes": 5}'
        )
        runbook_response.usage = mocker.MagicMock(input_tokens=80, output_tokens=40)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=[triage_response, runbook_response],
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        graph, state = build_graph(
            defn,
            workflow_id="wf-yaml-1",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            approval_policy={"auto_approve": True},
        )

        await graph.run(state=state)

        for key, result in state.step_results.items():
            assert result["status"] in ("completed", "skipped"), f"Step {key}: {result}"

    @pytest.mark.asyncio
    async def test_triage_pauses_without_auto_approve(
        self, mocker: MockerFixture
    ) -> None:
        """Workflow pauses at approval gate without auto_approve."""
        with open("examples/workflow-definitions/triage-classify-workflow.yaml") as f:
            defn = yaml.safe_load(f)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})

        mock_response = mocker.MagicMock()
        mock_response.text = (
            '{"severity": "high", "category": "resource",'
            ' "summary": "OOM", "recommended_action": "restart"}'
        )
        mock_response.usage = mocker.MagicMock(input_tokens=50, output_tokens=20)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        graph, state = build_graph(
            defn,
            workflow_id="wf-yaml-2",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            # No approval_policy — defaults to requiring manual approval
        )

        await graph.run(state=state)

        assert state.paused_at_step is not None

    @pytest.mark.asyncio
    async def test_all_example_yamls_build_graph(self) -> None:
        """Every YAML in examples/ builds a valid graph (regression guard)."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        yamls = glob.glob("examples/workflow-definitions/*.yaml")
        assert len(yamls) > 0, "No example YAMLs found"

        for path in yamls:
            with open(path) as f:
                defn = yaml.safe_load(f)

            graph, state = build_graph(
                defn,
                workflow_id=f"wf-{os.path.basename(path)}",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "k",
                },
            )
            assert graph is not None, f"Failed to build graph for {path}"

    @pytest.mark.asyncio
    async def test_triage_step_results_contain_output(
        self, mocker: MockerFixture
    ) -> None:
        """Step results contain structured output from the mocked LLM."""
        with open("examples/workflow-definitions/triage-classify-workflow.yaml") as f:
            defn = yaml.safe_load(f)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})

        triage_response = mocker.MagicMock()
        triage_response.text = (
            '{"severity": "critical", "category": "security",'
            ' "summary": "Unauthorized access attempt",'
            ' "recommended_action": "isolate node"}'
        )
        triage_response.usage = mocker.MagicMock(input_tokens=50, output_tokens=20)

        runbook_response = mocker.MagicMock()
        runbook_response.text = (
            '{"title": "Security Response", "steps": [{"action": "isolate",'
            ' "command": "kubectl cordon node-1",'
            ' "expected_output": "node cordoned"}]}'
        )
        runbook_response.usage = mocker.MagicMock(input_tokens=60, output_tokens=30)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=[triage_response, runbook_response],
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        graph, state = build_graph(
            defn,
            workflow_id="wf-yaml-3",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            approval_policy={"auto_approve": True},
        )

        await graph.run(state=state)

        triage_result = state.step_results["triage_result"]
        assert triage_result["status"] == "completed"
        assert triage_result["output"]["severity"] == "critical"
        assert triage_result["output"]["category"] == "security"
