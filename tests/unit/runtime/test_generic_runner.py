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
from agents.models import AgentRunRequest, DiagnosticReport
from agents.runtime.generic_runner import create_generic_runner
from examples.agents.diagnostic.cluster_state import init_scenario


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state before each test."""
    init_scenario("healthy")


def _make_spec(
    output_type: str = "DiagnosticReport",
    tools_module: str = "examples.agents.diagnostic.tools",
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
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="list_hosts", args="{}", tool_call_id="c1"
                        ),
                    ]
                )
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
                summary="ok",
                issues_found=[],
                actions_taken=[],
                cluster_healthy=True,
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

    @pytest.mark.asyncio
    async def test_advisory_mode_filters_tools(self) -> None:
        """Test that advisory mode only registers read-only tools."""
        tool_names_seen = []

        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_names_seen.extend(t.name for t in info.function_tools)
            report = DiagnosticReport(
                summary="ok",
                issues_found=[],
                actions_taken=[],
                cluster_healthy=True,
            )
            return ModelResponse(parts=[TextPart(content=report.model_dump_json())])

        spec = AgentSpec(
            instructions="You are a test agent.",
            output_type="DiagnosticReport",
            tools=ToolsSpec(
                module="examples.agents.diagnostic.tools",
                functions=["list_hosts", "check_host", "run_remediation"],
                read_only=["list_hosts", "check_host"],
            ),
            lifecycle=LifecycleSpec(type="request-response"),
        )
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        request = AgentRunRequest(
            prompt="Check hosts",
            context={"advisory_mode": True},
        )
        await runner(request)

        assert "list_hosts" in tool_names_seen
        assert "check_host" in tool_names_seen
        assert "run_remediation" not in tool_names_seen

    @pytest.mark.asyncio
    async def test_non_advisory_has_all_tools(self) -> None:
        """Test that non-advisory mode has all tools including write tools."""
        tool_names_seen = []

        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_names_seen.extend(t.name for t in info.function_tools)
            report = DiagnosticReport(
                summary="ok",
                issues_found=[],
                actions_taken=[],
                cluster_healthy=True,
            )
            return ModelResponse(parts=[TextPart(content=report.model_dump_json())])

        spec = AgentSpec(
            instructions="You are a test agent.",
            output_type="DiagnosticReport",
            tools=ToolsSpec(
                module="examples.agents.diagnostic.tools",
                functions=["list_hosts", "check_host", "run_remediation"],
                read_only=["list_hosts", "check_host"],
            ),
            lifecycle=LifecycleSpec(type="request-response"),
        )
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        request = AgentRunRequest(prompt="Check hosts")
        await runner(request)

        assert "list_hosts" in tool_names_seen
        assert "check_host" in tool_names_seen
        assert "run_remediation" in tool_names_seen
