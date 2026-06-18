"""Integration tests for the monitoring agent using FunctionModel."""

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agents.diagnostic.cluster_state import init_scenario
from agents.models import MonitoringResult
from agents.monitoring.agent import create_monitoring_agent


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state before each test."""
    init_scenario("healthy")


def _has_tool_returns(messages: list[ModelMessage]) -> bool:
    """Check if any message contains a ToolReturnPart."""
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                return True
    return False


class TestMonitoringAgentWithFunctionModel:
    """Integration tests using FunctionModel to mock the LLM."""

    @pytest.mark.asyncio
    async def test_healthy_cluster_returns_no_alerts(self) -> None:
        """Test that a healthy cluster produces no alerts."""
        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if not _has_tool_returns(messages):
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="get_cluster_summary", args="{}", tool_call_id="c1"),
                ])
            result = MonitoringResult(alerts=[], cluster_healthy=True)
            return ModelResponse(parts=[TextPart(content=result.model_dump_json())])

        agent = create_monitoring_agent(FunctionModel(mock_llm))
        result = await agent.run("Check cluster health")
        assert result.output.cluster_healthy is True
        assert result.output.alerts == []

    @pytest.mark.asyncio
    async def test_degraded_cluster_returns_alerts(self) -> None:
        """Test that a degraded cluster produces alerts."""
        init_scenario("bad_deploy")

        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if not _has_tool_returns(messages):
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="get_cluster_summary", args="{}", tool_call_id="c1"),
                ])
            result = MonitoringResult(
                alerts=[{
                    "host": "web-02",
                    "metric": "cpu",
                    "value": "92%",
                    "severity": "critical",
                    "context": "CPU spike after deploy",
                    "recommended_action": "investigate",
                }],
                cluster_healthy=False,
            )
            return ModelResponse(parts=[TextPart(content=result.model_dump_json())])

        agent = create_monitoring_agent(FunctionModel(mock_llm))
        result = await agent.run("Check cluster health")
        assert result.output.cluster_healthy is False
        assert len(result.output.alerts) == 1
        assert result.output.alerts[0].severity == "critical"

    @pytest.mark.asyncio
    async def test_monitoring_agent_has_no_remediation_tools(self) -> None:
        """Test that the monitoring agent does NOT have remediation tools."""
        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_names = [t.name for t in info.function_tools]
            assert "run_remediation" not in tool_names
            assert "check_host" not in tool_names
            result = MonitoringResult(alerts=[], cluster_healthy=True)
            return ModelResponse(parts=[TextPart(content=result.model_dump_json())])

        agent = create_monitoring_agent(FunctionModel(mock_llm))
        await agent.run("Check cluster health")

    @pytest.mark.asyncio
    async def test_uses_monitoring_result_output_type(self) -> None:
        """Test that the agent uses MonitoringResult as output type."""
        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if not _has_tool_returns(messages):
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="get_cluster_summary", args="{}", tool_call_id="c1"),
                ])
            result = MonitoringResult(alerts=[], cluster_healthy=True)
            return ModelResponse(parts=[TextPart(content=result.model_dump_json())])

        agent = create_monitoring_agent(FunctionModel(mock_llm))
        result = await agent.run("Check health")
        assert isinstance(result.output, MonitoringResult)
