"""Integration test — spawn: none step runs through the full graph translator."""

from __future__ import annotations

import os

import pytest
from pytest_mock import MockerFixture


class TestDirectExecutorGraphIntegration:
    """Verify DirectExecutor works end-to-end through graph_translator."""

    @pytest.mark.asyncio
    async def test_spawn_none_step_completes_via_graph(self, mocker: MockerFixture) -> None:
        """A spawn: none step runs through build_graph + graph.run without mocking the executor."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"severity": "high", "category": "resource"}',
                "input_tokens": 80,
                "output_tokens": 30,
            },
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "triage-test"},
            "spec": {
                "steps": [
                    {
                        "name": "triage",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Classify this alert",
                        "output_key": "triage_result",
                        "output_schema": {
                            "type": "object",
                            "properties": {
                                "severity": {"type": "string"},
                                "category": {"type": "string"},
                            },
                        },
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-direct-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        )

        result = await graph.run(state=state)
        assert state.step_results["triage_result"]["status"] == "completed"
        assert state.step_results["triage_result"]["output"]["severity"] == "high"

    @pytest.mark.asyncio
    async def test_mixed_spawn_none_and_approval(self, mocker: MockerFixture) -> None:
        """Workflow with spawn: none step + approval step works end-to-end."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"severity": "critical"}',
                "input_tokens": 50,
                "output_tokens": 20,
            },
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "triage-approve"},
            "spec": {
                "steps": [
                    {
                        "name": "triage",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Classify",
                        "output_key": "triage_result",
                    },
                    {
                        "name": "approve",
                        "type": "human-approval",
                        "output_key": "approval",
                        "message": "Approve?",
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-mixed-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
            approval_policy={"auto_approve": True},
        )

        await graph.run(state=state)
        assert state.step_results["triage_result"]["status"] == "completed"
        assert state.step_results["approval"]["status"] == "completed"
        assert state.step_results["approval"]["output"]["auto_approved"] is True

    @pytest.mark.asyncio
    async def test_spawn_none_no_spawner_needed(self, mocker: MockerFixture) -> None:
        """spawn: none works without any spawner — key value prop for embedded mode."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"answer": "42"}',
                "input_tokens": 10,
                "output_tokens": 5,
            },
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "no-infra"},
            "spec": {
                "steps": [
                    {
                        "name": "ask",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "What is the meaning of life?",
                        "output_key": "answer",
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-no-infra-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
            spawner=None,
        )

        await graph.run(state=state)
        assert state.step_results["answer"]["status"] == "completed"
        assert state.step_results["answer"]["output"]["answer"] == "42"

    @pytest.mark.asyncio
    async def test_spawn_none_condition_skips(self, mocker: MockerFixture) -> None:
        """spawn: none step with false condition is skipped."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        call_count = 0

        async def mock_call_llm(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "content": '{"severity": "low"}',
                "input_tokens": 10,
                "output_tokens": 5,
            }

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            side_effect=mock_call_llm,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "cond-test"},
            "spec": {
                "steps": [
                    {
                        "name": "triage",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Classify",
                        "output_key": "triage_result",
                    },
                    {
                        "name": "escalate",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Escalate",
                        "output_key": "escalation",
                        "condition": "steps.triage_result.output.severity == 'critical'",
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-cond-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        )

        await graph.run(state=state)
        assert state.step_results["triage_result"]["status"] == "completed"
        assert state.step_results["escalation"]["status"] == "skipped"
        assert call_count == 1
