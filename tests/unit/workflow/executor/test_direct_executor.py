"""Tests for DirectExecutor — spawn: none LLM-only step executor."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


class TestDirectExecutorInstantiation:
    """Tests for DirectExecutor creation."""

    def test_implements_step_executor(self) -> None:
        """DirectExecutor is a StepExecutor."""
        from cloud_agents.workflow.executor.step.base import StepExecutor
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        assert issubclass(DirectExecutor, StepExecutor)

    def test_construction(self) -> None:
        """DirectExecutor can be instantiated with no arguments."""
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        executor = DirectExecutor()
        assert executor is not None

    def test_no_temporal_imports(self) -> None:
        """DirectExecutor has zero temporalio imports."""
        from cloud_agents.workflow.executor.step import direct as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source


def _mock_model_response(
    mocker: MockerFixture, text: str, input_tokens: int = 0, output_tokens: int = 0
) -> AsyncMock:
    """Create a mock for pydantic_ai.direct.model_request that returns a ModelResponse.

    Parameters:
        mocker: Pytest mocker fixture.
        text: Text content of the response.
        input_tokens: Input token count for usage.
        output_tokens: Output token count for usage.

    Returns:
        The mock object.
    """
    from pydantic_ai.usage import RequestUsage

    mock_response = mocker.MagicMock()
    mock_response.text = text
    mock_usage = RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    mock_response.usage = mock_usage

    mock_fn = mocker.patch(
        "cloud_agents.workflow.executor.step.direct.model_request",
        new_callable=AsyncMock,
        return_value=mock_response,
    )
    return mock_fn


class TestDirectExecutorRun:
    """Tests for DirectExecutor.run() — LLM-only execution."""

    @pytest.mark.asyncio
    async def test_calls_llm_and_returns_result(self, mocker: MockerFixture) -> None:
        """DirectExecutor calls the LLM and returns structured output."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text='{"severity": "high", "category": "security"}',
            input_tokens=100,
            output_tokens=50,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Classify this alert by severity",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "openai-api-key",
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string"},
                        "category": {"type": "string"},
                    },
                },
                workflow_id="wf-1",
                step_name="triage",
                output_key="triage_result",
            )
        )

        assert result.status == "completed"
        assert result.output == {"severity": "high", "category": "security"}
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self, mocker: MockerFixture) -> None:
        """DirectExecutor includes system prompt in messages."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Classify this alert",
                system_prompt="You are a security analyst.",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert any(m["role"] == "system" for m in messages)
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "security analyst" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_includes_context_in_prompt(self, mocker: MockerFixture) -> None:
        """DirectExecutor includes prior step context in the user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Based on the diagnosis, recommend a fix",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context={
                    "diagnosis": {
                        "status": "completed",
                        "output": {"severity": "high", "issue": "OOM"},
                    },
                },
            )
        )

        user_msg = next(m for m in messages if m["role"] == "user")
        assert "OOM" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_plain_text_response_without_schema(self, mocker: MockerFixture) -> None:
        """DirectExecutor returns plain text wrapped in dict when no output_schema."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text="The alert is a false positive.",
            input_tokens=40,
            output_tokens=15,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Summarize this alert",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert result.status == "completed"
        assert result.output == {"response": "The alert is a false positive."}

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self, mocker: MockerFixture) -> None:
        """DirectExecutor returns failed status on LLM error."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=Exception("API rate limit exceeded"),
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Classify this alert",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert result.status == "failed"
        assert "rate limit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_invalid_json_response_with_schema_fails(self, mocker: MockerFixture) -> None:
        """DirectExecutor fails when output_schema is set but LLM returns non-JSON."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text="Not valid JSON",
            input_tokens=30,
            output_tokens=10,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Classify this",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                output_schema={"type": "object"},
            )
        )

        assert result.status == "failed"
        assert "non-json" in result.error.lower()

    @pytest.mark.asyncio
    async def test_transcript_records_llm_call(self, mocker: MockerFixture) -> None:
        """DirectExecutor records the LLM call in transcript."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=50,
            output_tokens=20,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Check it",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                step_name="check",
            )
        )

        assert len(result.transcript) >= 1
        assert any(e.get("type") == "llm.call" for e in result.transcript)

    @pytest.mark.asyncio
    async def test_duration_tracked(self, mocker: MockerFixture) -> None:
        """DirectExecutor tracks execution duration."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=50,
            output_tokens=20,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Check",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_anthropic_provider_works(self, mocker: MockerFixture) -> None:
        """Anthropic provider works natively via pydantic-ai (no longer rejected)."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=5,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="test",
                provider={
                    "name": "anthropic",
                    "model": "claude-sonnet-5",
                    "credentials_secret": "anthropic-api-key",
                },
            )
        )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_model_request_called_with_correct_model_string(
        self, mocker: MockerFixture
    ) -> None:
        """model_request is called with correct pydantic-ai model string."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        mock_fn = _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=5,
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        mock_fn.assert_called_once()
        call_args = mock_fn.call_args
        assert call_args[0][0] == "openai:gpt-4o"
        assert call_args.kwargs["model_settings"]["timeout"] == 600


class TestDirectExecutorDispatch:
    """Tests for step dispatch integration."""

    def test_dispatch_returns_direct_executor(self) -> None:
        """get_step_executor returns DirectExecutor for spawn: none."""
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        step = {"name": "triage", "type": "agent", "spawn": "none"}
        executor = get_step_executor(step, spawner=None)
        assert isinstance(executor, DirectExecutor)

    def test_dispatch_no_spawner_needed(self) -> None:
        """spawn: none works without any spawner configured."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        step = {"name": "triage", "type": "agent", "spawn": "none"}
        executor = get_step_executor(step, spawner=None)
        assert executor is not None


class TestBuildMessages:
    """Tests for message construction."""

    def test_basic_prompt_only(self) -> None:
        """Builds a single user message from prompt."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Hello",
                provider={"name": "openai", "model": "gpt-4o"},
            )
        )

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"

    def test_system_prompt_added_first(self) -> None:
        """System prompt appears before user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Hello",
                system_prompt="Be helpful",
                provider={"name": "openai", "model": "gpt-4o"},
            )
        )

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_output_schema_appended_to_user_message(self) -> None:
        """Output schema instructions appended to user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Classify",
                provider={"name": "openai", "model": "gpt-4o"},
                output_schema={
                    "type": "object",
                    "properties": {"severity": {"type": "string"}},
                },
            )
        )

        user_content = messages[-1]["content"]
        assert "JSON" in user_content
        assert "severity" in user_content

    def test_context_included_in_user_message(self) -> None:
        """Prior step outputs included in user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Fix the issue",
                provider={"name": "openai", "model": "gpt-4o"},
                context={
                    "diagnosis": {
                        "status": "completed",
                        "output": {"issue": "disk full"},
                    },
                },
            )
        )

        user_content = messages[-1]["content"]
        assert "disk full" in user_content

    def test_empty_context_not_added(self) -> None:
        """Empty context dict doesn't add context block."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Hello",
                provider={"name": "openai", "model": "gpt-4o"},
                context={},
            )
        )

        assert "Prior step" not in messages[-1]["content"]


class TestParseOutput:
    """Tests for output parsing."""

    def test_valid_json_parsed(self) -> None:
        """Valid JSON string is parsed to dict."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        result = _parse_output('{"severity": "high"}', {"type": "object"})
        assert result == {"severity": "high"}

    def test_invalid_json_with_schema_raises(self) -> None:
        """Invalid JSON raises ValueError when output_schema is set."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        with pytest.raises(ValueError, match="non-JSON"):
            _parse_output("Not JSON", {"type": "object"})

    def test_no_schema_parses_json(self) -> None:
        """Without schema, still attempts JSON parse."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        result = _parse_output('{"ok": true}', None)
        assert result == {"ok": True}

    def test_no_schema_plain_text(self) -> None:
        """Without schema, plain text wrapped in response dict."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        result = _parse_output("Just text", None)
        assert result == {"response": "Just text"}

    def test_none_content_without_schema(self) -> None:
        """None content without schema returns response: None."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        result = _parse_output(None, None)
        assert result == {"response": None}

    def test_none_content_with_schema_raises(self) -> None:
        """None content with output_schema raises ValueError."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        with pytest.raises(ValueError, match="null content"):
            _parse_output(None, {"type": "object"})


@pytest.fixture(autouse=True)
def _clean_tool_registry() -> None:
    """Clear tool registry before each test."""
    from cloud_agents.workflow.executor.step.tools import clear_tools

    clear_tools()
    yield  # type: ignore[misc]
    clear_tools()


def _dummy_tool(query: str) -> str:
    """A dummy tool for testing.

    Parameters:
        query: Input query.

    Returns:
        Fixed string result.
    """
    return f"result: {query}"


class TestDirectExecutorWithTools:
    """Tests for DirectExecutor.run() when tools are present."""

    @pytest.mark.asyncio
    async def test_uses_agent_when_tools_present(self, mocker: MockerFixture) -> None:
        """DirectExecutor uses pydantic-ai Agent when step has tools."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool, description="Get K8s resources")

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 80
        mock_usage.output_tokens = 30

        mock_result = mocker.MagicMock()
        mock_result.output = '{"severity": "high"}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Get pods in default namespace",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "openai-api-key",
                },
                tools=["kubectl_get"],
                workflow_id="wf-1",
                step_name="get-pods",
                output_key="pods",
            )
        )

        assert result.status == "completed"
        mock_agent_cls.assert_called_once()
        mock_agent_instance.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_created_with_correct_tools(self, mocker: MockerFixture) -> None:
        """Agent is created with tools from the registry."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
            )
        )

        # Check Agent was constructed with tools kwarg
        call_kwargs = mock_agent_cls.call_args
        assert "tools" in call_kwargs.kwargs
        assert len(call_kwargs.kwargs["tools"]) == 1

    @pytest.mark.asyncio
    async def test_agent_uses_correct_model_string(self, mocker: MockerFixture) -> None:
        """Agent is created with correct pydantic-ai model string."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={
                    "name": "anthropic",
                    "model": "claude-sonnet-5",
                    "credentials_secret": "k",
                },
                tools=["kubectl_get"],
            )
        )

        # First positional arg to Agent() is the model string
        call_args = mock_agent_cls.call_args
        assert call_args[0][0] == "anthropic:claude-sonnet-5"

    @pytest.mark.asyncio
    async def test_no_tools_uses_model_request(self, mocker: MockerFixture) -> None:
        """Without tools, DirectExecutor still uses model_request (existing path)."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        mock_fn = _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=5,
        )

        # Ensure Agent is NOT called
        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=[],
            )
        )

        assert result.status == "completed"
        mock_fn.assert_called_once()
        mock_agent_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_failed(self, mocker: MockerFixture) -> None:
        """DirectExecutor returns failed status when a tool name is unknown."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["nonexistent_tool"],
            )
        )

        assert result.status == "failed"
        assert "Unknown tool" in result.error

    @pytest.mark.asyncio
    async def test_agent_tracks_token_usage(self, mocker: MockerFixture) -> None:
        """DirectExecutor extracts token usage from Agent result."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 120
        mock_usage.output_tokens = 45

        mock_result = mocker.MagicMock()
        mock_result.output = "The pods are running fine."
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Check pods",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
            )
        )

        assert result.input_tokens == 120
        assert result.output_tokens == 45

    @pytest.mark.asyncio
    async def test_agent_includes_system_prompt(self, mocker: MockerFixture) -> None:
        """Agent is created with instructions from system_prompt."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Check pods",
                system_prompt="You are a K8s expert.",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert call_kwargs["instructions"] == "You are a K8s expert."


class TestDirectExecutorWithMCPServers:
    """Tests for DirectExecutor.run() when MCP servers are configured."""

    @pytest.mark.asyncio
    async def test_mcp_servers_only_dispatches_to_agent(self, mocker: MockerFixture) -> None:
        """Step with mcp_servers but no tools should use Agent path."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_result = mocker.MagicMock()
        mock_result.output = '{"status": "ok"}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        # Ensure model_request is NOT called (Agent path is used)
        mock_model_req = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Query the cluster",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                ],
            )
        )

        assert result.status == "completed"
        mock_agent_cls.assert_called_once()
        mock_model_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_servers_with_tools(self, mocker: MockerFixture) -> None:
        """Step with both tools and mcp_servers uses Agent path with both."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool, description="Get K8s resources")

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 60
        mock_usage.output_tokens = 25

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Query the cluster",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                ],
            )
        )

        assert result.status == "completed"

        # Agent should be created with both tools and toolsets
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) == 1
        assert "toolsets" in call_kwargs
        assert len(call_kwargs["toolsets"]) == 1

    @pytest.mark.asyncio
    async def test_mcp_servers_with_auth_headers(self, mocker: MockerFixture) -> None:
        """MCP server auth headers are passed to StreamableHttpTransport."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mock_transport_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Query the cluster",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[
                    {
                        "name": "kubectl",
                        "url": "http://mcp-kubectl:8080/sse",
                        "headers": {"Authorization": "Bearer secret-token"},
                    },
                ],
            )
        )

        # Verify StreamableHttpTransport was called with correct URL and headers
        mock_transport_cls.assert_called_once_with(
            url="http://mcp-kubectl:8080/sse",
            headers={"Authorization": "Bearer secret-token"},
        )

    @pytest.mark.asyncio
    async def test_mcp_toolset_context_manager_lifecycle(self, mocker: MockerFixture) -> None:
        """MCPToolset context manager is properly entered and exited."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Query",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                ],
            )
        )

        # Verify the context manager was entered and exited
        mock_toolset.__aenter__.assert_called_once()
        mock_toolset.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_mcp_servers(self, mocker: MockerFixture) -> None:
        """Multiple MCP servers each get their own MCPToolset."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset_1 = mocker.MagicMock()
        mock_toolset_1.__aenter__ = mocker.AsyncMock(return_value=mock_toolset_1)
        mock_toolset_1.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_toolset_2 = mocker.MagicMock()
        mock_toolset_2.__aenter__ = mocker.AsyncMock(return_value=mock_toolset_2)
        mock_toolset_2.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            side_effect=[mock_toolset_1, mock_toolset_2],
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Query",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                    {"name": "rhdh", "url": "http://mcp-rhdh:8080/sse"},
                ],
            )
        )

        # Agent should receive both toolsets
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "toolsets" in call_kwargs
        assert len(call_kwargs["toolsets"]) == 2

    @pytest.mark.asyncio
    async def test_no_mcp_servers_no_tools_uses_model_request(self, mocker: MockerFixture) -> None:
        """Without MCP servers or tools, model_request path is used."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        mock_fn = _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=5,
        )

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=None,
            )
        )

        assert result.status == "completed"
        mock_fn.assert_called_once()
        mock_agent_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_mcp_servers_list_uses_model_request(self, mocker: MockerFixture) -> None:
        """Empty mcp_servers list should use model_request path."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        mock_fn = _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=5,
        )

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[],
            )
        )

        assert result.status == "completed"
        mock_fn.assert_called_once()
        mock_agent_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_servers_recorded_in_transcript(self, mocker: MockerFixture) -> None:
        """MCP server names are recorded in the transcript."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Query",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                ],
                step_name="query-step",
            )
        )

        assert len(result.transcript) >= 1
        transcript_entry = result.transcript[0]
        assert "mcp_servers" in transcript_entry
        assert transcript_entry["mcp_servers"] == ["kubectl"]


async def _async_iter(items: list[str]):
    """Async iterator helper for mocking stream_text.

    Parameters:
        items: List of string items to yield.

    Yields:
        Each item from the list.
    """
    for item in items:
        yield item


class TestDirectExecutorStreaming:
    """Tests for DirectExecutor.run_stream() — streaming token output."""

    @pytest.mark.asyncio
    async def test_run_stream_with_tools_yields_tokens_then_complete(
        self, mocker: MockerFixture
    ) -> None:
        """run_stream() with tools yields token events then complete event."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool, description="Get K8s resources")

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 80
        mock_usage.output_tokens = 30

        mock_streamed = mocker.MagicMock()
        mock_streamed.stream_text = mocker.MagicMock(return_value=_async_iter(["Hello", " world"]))
        mock_streamed.get_output = mocker.AsyncMock(return_value="Hello world")
        mock_streamed.usage = mock_usage
        mock_streamed.__aenter__ = mocker.AsyncMock(return_value=mock_streamed)
        mock_streamed.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(return_value=mock_streamed)
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        events: list[StreamEvent] = []
        async for event in executor.run_stream(
            StepInput(
                prompt="Get pods",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "openai-api-key",
                },
                tools=["kubectl_get"],
                workflow_id="wf-1",
                step_name="get-pods",
                output_key="pods",
            )
        ):
            events.append(event)

        # Should have token events for each delta, then a complete event
        token_events = [e for e in events if e.type == "token"]
        complete_events = [e for e in events if e.type == "complete"]

        assert len(token_events) == 2
        assert token_events[0].data == {"delta": "Hello"}
        assert token_events[1].data == {"delta": " world"}
        assert len(complete_events) == 1
        assert complete_events[0].result is not None
        assert complete_events[0].result.status == "completed"
        assert len(complete_events[0].result.transcript) >= 1
        assert complete_events[0].result.transcript[0]["type"] == "agent.stream"
        assert complete_events[0].result.input_tokens == 80
        assert complete_events[0].result.output_tokens == 30

    @pytest.mark.asyncio
    async def test_run_stream_without_tools_yields_single_complete(
        self, mocker: MockerFixture
    ) -> None:
        """run_stream() without tools falls back to default (single complete event)."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )
        _mock_model_response(
            mocker,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=5,
        )

        executor = DirectExecutor()
        events: list[StreamEvent] = []
        async for event in executor.run_stream(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        ):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "complete"
        assert events[0].result.status == "completed"

    @pytest.mark.asyncio
    async def test_run_stream_with_mcp_servers_yields_tokens(self, mocker: MockerFixture) -> None:
        """run_stream() with MCP servers uses Agent streaming path."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_streamed = mocker.MagicMock()
        mock_streamed.stream_text = mocker.MagicMock(return_value=_async_iter(["chunk1", "chunk2"]))
        mock_streamed.get_output = mocker.AsyncMock(return_value="chunk1chunk2")
        mock_streamed.usage = mock_usage
        mock_streamed.__aenter__ = mocker.AsyncMock(return_value=mock_streamed)
        mock_streamed.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(return_value=mock_streamed)
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        executor = DirectExecutor()
        events: list[StreamEvent] = []
        async for event in executor.run_stream(
            StepInput(
                prompt="Query cluster",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                ],
            )
        ):
            events.append(event)

        token_events = [e for e in events if e.type == "token"]
        complete_events = [e for e in events if e.type == "complete"]

        assert len(token_events) == 2
        assert len(complete_events) == 1

    @pytest.mark.asyncio
    async def test_run_stream_error_yields_error_event(self, mocker: MockerFixture) -> None:
        """run_stream() error yields an error event with failed StepResult."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(side_effect=RuntimeError("API timeout"))
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        events: list[StreamEvent] = []
        async for event in executor.run_stream(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
            )
        ):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"
        assert "API timeout" in events[0].data["error"]
        assert events[0].result is not None
        assert events[0].result.status == "failed"

    @pytest.mark.asyncio
    async def test_run_stream_complete_has_tokens_and_output(self, mocker: MockerFixture) -> None:
        """Complete event has StepResult with output, input_tokens, output_tokens."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 120
        mock_usage.output_tokens = 45

        mock_streamed = mocker.MagicMock()
        mock_streamed.stream_text = mocker.MagicMock(return_value=_async_iter(["result"]))
        mock_streamed.get_output = mocker.AsyncMock(return_value='{"severity": "high"}')
        mock_streamed.usage = mock_usage
        mock_streamed.__aenter__ = mocker.AsyncMock(return_value=mock_streamed)
        mock_streamed.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(return_value=mock_streamed)
        mock_agent_cls.return_value = mock_agent_instance

        executor = DirectExecutor()
        events: list[StreamEvent] = []
        async for event in executor.run_stream(
            StepInput(
                prompt="Check",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
                output_schema={"type": "object", "properties": {"severity": {"type": "string"}}},
            )
        ):
            events.append(event)

        complete_events = [e for e in events if e.type == "complete"]
        assert len(complete_events) == 1
        result = complete_events[0].result
        assert result.status == "completed"
        assert result.output == {"severity": "high"}
        assert result.input_tokens == 120
        assert result.output_tokens == 45
        assert result.duration_ms >= 0


class TestFalsyOutputPreservation:
    """Tests for preserving falsy prior-step outputs."""

    def test_empty_list_output_included(self) -> None:
        """Prior step output of [] is included in context."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Check results",
                provider={"name": "openai", "model": "gpt-4o"},
                context={
                    "scan": {
                        "status": "completed",
                        "output": [],
                    },
                },
            )
        )

        user_content = messages[-1]["content"]
        assert "Prior step" in user_content
        assert "[]" in user_content

    def test_zero_output_included(self) -> None:
        """Prior step output of 0 is included in context."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(
            StepInput(
                prompt="Check count",
                provider={"name": "openai", "model": "gpt-4o"},
                context={
                    "count": {
                        "status": "completed",
                        "output": 0,
                    },
                },
            )
        )

        user_content = messages[-1]["content"]
        assert "Prior step" in user_content


class TestDirectExecutorWithSkills:
    """Tests for DirectExecutor skills capability integration."""

    @pytest.mark.asyncio
    async def test_agent_receives_capabilities_when_skills_path_set(
        self, mocker: MockerFixture
    ) -> None:
        """Agent is created with capabilities when CLOUD_AGENTS_SKILLS_PATHS is set."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_cap = mocker.MagicMock()
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability",
            return_value=mock_cap,
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "capabilities" in call_kwargs
        assert call_kwargs["capabilities"] == [mock_cap]

    @pytest.mark.asyncio
    async def test_skills_only_uses_agent_path(self, mocker: MockerFixture) -> None:
        """Skills-only step (no tools, no MCP) routes to Agent path."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 40
        mock_usage.output_tokens = 15

        mock_result = mocker.MagicMock()
        mock_result.output = '{"answer": "42"}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_cap = mocker.MagicMock()
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability",
            return_value=mock_cap,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Use a skill to answer",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert result.status == "completed"
        mock_agent_cls.assert_called_once()
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "capabilities" in call_kwargs
        assert call_kwargs["capabilities"] == [mock_cap]

    @pytest.mark.asyncio
    async def test_agent_no_capabilities_when_skills_path_unset(
        self, mocker: MockerFixture
    ) -> None:
        """Agent has no capabilities when CLOUD_AGENTS_SKILLS_PATHS is unset."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability",
            return_value=None,
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert call_kwargs.get("capabilities") is None

    @pytest.mark.asyncio
    async def test_skills_capability_alongside_tools_and_mcp(self, mocker: MockerFixture) -> None:
        """Skills capability passed alongside tools and MCP toolsets."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool, description="Get K8s resources")

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 60
        mock_usage.output_tokens = 25

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.StreamableHttpTransport",
        )

        mock_cap = mocker.MagicMock()
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability",
            return_value=mock_cap,
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Query the cluster",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                tools=["kubectl_get"],
                mcp_servers=[
                    {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
                ],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        # All three: tools, toolsets, and capabilities
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) == 1
        assert "toolsets" in call_kwargs
        assert len(call_kwargs["toolsets"]) == 1
        assert "capabilities" in call_kwargs
        assert call_kwargs["capabilities"] == [mock_cap]
