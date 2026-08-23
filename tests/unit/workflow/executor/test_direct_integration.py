"""Integration test — spawn: none step runs through the full graph translator."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

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


class TestDirectExecutorToolsGraphIntegration:
    """Verify spawn: none with tools works end-to-end through graph_translator.

    These tests exercise the full chain: build_graph → StepInput → DirectExecutor
    → _run_with_agent, catching wiring gaps that unit tests with mocked executors miss.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Any:
        """Clear tool registry before and after each test."""
        from cloud_agents.workflow.executor.step.tools import clear_tools

        clear_tools()
        yield
        clear_tools()

    @pytest.mark.asyncio
    async def test_spawn_none_with_tools_via_graph(self, mocker: MockerFixture) -> None:
        """spawn: none step with tools runs Agent through the full graph chain."""
        from cloud_agents.workflow.executor.step.tools import register_tool

        def echo_tool(message: str) -> str:
            """Echo back."""
            return f"echo: {message}"

        register_tool("echo_tool", echo_tool)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_result = mocker.MagicMock()
        mock_result.output = '{"action": "restart"}'
        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20
        mock_result.usage = mock_usage

        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run = AsyncMock(return_value=mock_result)
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
            return_value=mock_agent_instance,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "tools-test"},
            "spec": {
                "steps": [
                    {
                        "name": "investigate",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Investigate the alert",
                        "output_key": "investigation",
                        "tools": ["echo_tool"],
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-tools-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        )

        await graph.run(state=state)
        assert state.step_results["investigation"]["status"] == "completed"
        assert state.step_results["investigation"]["output"]["action"] == "restart"
        mock_agent_instance.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_none_unknown_tool_fails_via_graph(self, mocker: MockerFixture) -> None:
        """spawn: none step with unknown tool fails through the full graph chain."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "bad-tool-test"},
            "spec": {
                "steps": [
                    {
                        "name": "investigate",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Investigate",
                        "output_key": "result",
                        "tools": ["nonexistent_tool"],
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-bad-tool-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        )

        await graph.run(state=state)
        assert state.step_results["result"]["status"] == "failed"
        assert "Unknown tool" in state.step_results["result"]["error"]

    @pytest.mark.asyncio
    async def test_mixed_tools_and_no_tools_via_graph(self, mocker: MockerFixture) -> None:
        """Workflow with both tool and no-tool steps works end-to-end."""
        from cloud_agents.workflow.executor.step.tools import register_tool

        def my_tool(query: str) -> str:
            """Query something."""
            return "result"

        register_tool("my_tool", my_tool)

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_result = mocker.MagicMock()
        mock_result.output = '{"found": true}'
        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 30
        mock_usage.output_tokens = 10
        mock_result.usage = mock_usage

        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run = AsyncMock(return_value=mock_result)
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
            return_value=mock_agent_instance,
        )

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"severity": "high"}',
                "input_tokens": 50,
                "output_tokens": 20,
            },
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "mixed-test"},
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
                        "name": "investigate",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "Investigate",
                        "output_key": "investigation",
                        "tools": ["my_tool"],
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-mixed-tools-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        )

        await graph.run(state=state)
        assert state.step_results["triage_result"]["status"] == "completed"
        assert state.step_results["investigation"]["status"] == "completed"
        assert state.step_results["investigation"]["output"]["found"] is True
