"""Unit tests for generic agent runner."""

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agents.definition import AgentSpec, LifecycleSpec, ToolsSpec
from agents.diagnostic.cluster_state import init_scenario
from agents.models import AgentRunRequest, DiagnosticReport
from agents.runtime.generic_runner import create_generic_runner


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state before each test."""
    init_scenario("healthy")


def _make_spec(
    output_type: str = "DiagnosticReport",
    tools_module: str = "agents.diagnostic.tools",
    functions: list[str] | None = None,
) -> AgentSpec:
    """Create an AgentSpec for testing."""
    return AgentSpec(
        instructions="You are a test agent.",
        output_type=output_type,
        tools=ToolsSpec(
            module=tools_module,
            functions=functions or ["list_hosts"],
        ),
        lifecycle=LifecycleSpec(type="request-response"),
    )


def _has_tool_returns(messages: list[ModelMessage]) -> bool:
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                return True
    return False


class TestCreateGenericRunner:
    """Tests for create_generic_runner."""

    @pytest.mark.asyncio
    async def test_success_path(self) -> None:
        """Test that the runner produces a successful AgentRunResponse."""
        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if not _has_tool_returns(messages):
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="list_hosts", args="{}", tool_call_id="c1"),
                ])
            report = DiagnosticReport(
                summary="All good",
                issues_found=[],
                actions_taken=[],
                cluster_healthy=True,
            )
            return ModelResponse(parts=[TextPart(content=report.model_dump_json())])

        spec = _make_spec()
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        request = AgentRunRequest(prompt="Check hosts")
        response = await runner(request)

        assert response.success is True
        assert response.agent_name == "test-agent"
        assert response.output_type == "DiagnosticReport"
        assert response.output["cluster_healthy"] is True

    @pytest.mark.asyncio
    async def test_error_path(self) -> None:
        """Test that the runner handles agent errors gracefully."""
        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise RuntimeError("LLM exploded")

        spec = _make_spec()
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        request = AgentRunRequest(prompt="Check hosts")
        response = await runner(request)

        assert response.success is False
        assert "LLM exploded" in response.error
        assert response.output_type == "error"

    @pytest.mark.asyncio
    async def test_tools_are_registered(self) -> None:
        """Test that tools from the spec are registered on the agent."""
        tool_names_seen = []

        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_names_seen.extend(t.name for t in info.function_tools)
            report = DiagnosticReport(
                summary="ok", issues_found=[], actions_taken=[], cluster_healthy=True,
            )
            return ModelResponse(parts=[TextPart(content=report.model_dump_json())])

        spec = _make_spec(functions=["list_hosts", "check_host"])
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        await runner(AgentRunRequest(prompt="test"))

        assert "list_hosts" in tool_names_seen
        assert "check_host" in tool_names_seen

    @pytest.mark.asyncio
    async def test_str_output_type(self) -> None:
        """Test that str output type works for simple agents."""
        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="Hello world")])

        spec = _make_spec(output_type="str")
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        response = await runner(AgentRunRequest(prompt="Say hello"))

        assert response.success is True
        assert response.output_type == "str"
