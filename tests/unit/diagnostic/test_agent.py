"""Integration tests for the diagnostic agent using FunctionModel."""

import json

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agents.diagnostic.agent import create_diagnostic_agent, run_diagnostic
from agents.diagnostic.cluster_state import (
    reset_cluster_healthy,
    simulate_bad_deploy,
    cluster_state,
)
from agents.models import AgentRunRequest, DiagnosticReport


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state before each test."""
    reset_cluster_healthy()


def _has_tool_returns(messages: list[ModelMessage]) -> bool:
    """Check if any message contains a ToolReturnPart."""
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                return True
    return False


def _get_tool_return_names(messages: list[ModelMessage]) -> list[str]:
    """Extract tool names from ToolReturnPart entries in messages."""
    names = []
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                names.append(part.tool_name)
    return names


def _make_diagnostic_report_json(
    summary: str = "Fixed all issues",
    issues: list[str] | None = None,
    cluster_healthy: bool = True,
) -> str:
    """Create a valid DiagnosticReport JSON string for the mock LLM."""
    report = DiagnosticReport(
        summary=summary,
        issues_found=issues or ["web-02: app crashed"],
        actions_taken=[
            {"host": "web-02", "action": "restart_service:app", "result": "restarted", "success": True}
        ],
        cluster_healthy=cluster_healthy,
    )
    return report.model_dump_json()


class TestDiagnosticAgentWithFunctionModel:
    """Integration tests using FunctionModel to mock the LLM."""

    @pytest.mark.asyncio
    async def test_agent_calls_tools_and_returns_report(self) -> None:
        """Test that the agent calls tools and produces a DiagnosticReport."""
        simulate_bad_deploy()
        call_count = 0

        def mock_llm(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            tool_returns = _get_tool_return_names(messages)

            if call_count == 1:
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="list_hosts", args="{}", tool_call_id="c1"),
                ])
            if call_count == 2:
                return ModelResponse(parts=[
                    ToolCallPart(
                        tool_name="check_host",
                        args='{"hostname": "web-02"}',
                        tool_call_id="c2",
                    ),
                ])
            if call_count == 3:
                return ModelResponse(parts=[
                    ToolCallPart(
                        tool_name="run_remediation",
                        args='{"hostname": "web-02", "action": "restart_service:app", "reason": "app crashed"}',
                        tool_call_id="c3",
                    ),
                ])
            if call_count == 4:
                return ModelResponse(parts=[
                    ToolCallPart(
                        tool_name="check_host",
                        args='{"hostname": "web-02"}',
                        tool_call_id="c4",
                    ),
                ])
            return ModelResponse(parts=[
                TextPart(content=_make_diagnostic_report_json()),
            ])

        agent = create_diagnostic_agent(FunctionModel(mock_llm))
        result = await agent.run("Diagnose the cluster")

        assert isinstance(result.output, DiagnosticReport)
        assert result.output.cluster_healthy is True
        assert len(result.output.actions_taken) >= 1
        assert call_count >= 4

    @pytest.mark.asyncio
    async def test_agent_produces_valid_report_structure(self) -> None:
        """Test that the agent output has all required fields."""
        def mock_llm(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            if not _has_tool_returns(messages):
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="list_hosts", args="{}", tool_call_id="c1"),
                ])
            return ModelResponse(parts=[
                TextPart(content=_make_diagnostic_report_json(
                    summary="All hosts healthy",
                    issues=["none found"],
                )),
            ])

        agent = create_diagnostic_agent(FunctionModel(mock_llm))
        result = await agent.run("Check cluster health")

        report = result.output
        assert hasattr(report, "summary")
        assert hasattr(report, "issues_found")
        assert hasattr(report, "actions_taken")
        assert hasattr(report, "remaining_issues")
        assert hasattr(report, "cluster_healthy")

    @pytest.mark.asyncio
    async def test_tools_mutate_cluster_state(self) -> None:
        """Test that tool execution actually changes simulated state."""
        simulate_bad_deploy()
        assert cluster_state["hosts"]["web-02"]["status"] == "degraded"

        call_count = 0

        def mock_llm(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ModelResponse(parts=[
                    ToolCallPart(
                        tool_name="run_remediation",
                        args='{"hostname": "web-02", "action": "restart_service:app", "reason": "fix"}',
                        tool_call_id="c1",
                    ),
                ])
            return ModelResponse(parts=[
                TextPart(content=_make_diagnostic_report_json()),
            ])

        agent = create_diagnostic_agent(FunctionModel(mock_llm))
        await agent.run("Fix web-02")

        assert cluster_state["hosts"]["web-02"]["status"] == "healthy"
        assert cluster_state["hosts"]["web-02"]["services"]["app"] == "running"


class TestRunDiagnostic:
    """Tests for the run_diagnostic agent_runner function."""

    @pytest.mark.asyncio
    async def test_run_diagnostic_success(self, mocker: pytest.MonkeyPatch) -> None:
        """Test run_diagnostic returns AgentRunResponse on success."""
        simulate_bad_deploy()

        def mock_llm(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            if not _has_tool_returns(messages):
                return ModelResponse(parts=[
                    ToolCallPart(
                        tool_name="run_remediation",
                        args='{"hostname": "web-02", "action": "restart_service:app", "reason": "fix"}',
                        tool_call_id="c1",
                    ),
                ])
            return ModelResponse(parts=[
                TextPart(content=_make_diagnostic_report_json()),
            ])

        mocker.patch(
            "agents.diagnostic._model.get_model",
            return_value=FunctionModel(mock_llm),
        )

        request = AgentRunRequest(prompt="Fix the cluster")
        response = await run_diagnostic(request)

        assert response.success is True
        assert response.agent_name == "diagnostic-agent"
        assert response.output_type == "DiagnosticReport"
        assert response.output["cluster_healthy"] is True

    @pytest.mark.asyncio
    async def test_run_diagnostic_error(self, mocker: pytest.MonkeyPatch) -> None:
        """Test run_diagnostic returns error response on failure."""
        mocker.patch(
            "agents.diagnostic._model.get_model",
            side_effect=RuntimeError("No LLM backend"),
        )

        request = AgentRunRequest(prompt="Check hosts")
        response = await run_diagnostic(request)

        assert response.success is False
        assert "No LLM backend" in response.error
        assert response.output_type == "error"
