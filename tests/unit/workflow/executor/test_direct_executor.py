"""Tests for DirectExecutor — spawn: none LLM-only step executor."""

from __future__ import annotations

import os
from typing import Any

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


class TestDirectExecutorRun:
    """Tests for DirectExecutor.run() — LLM-only execution."""

    @pytest.mark.asyncio
    async def test_calls_llm_and_returns_result(self, mocker: MockerFixture) -> None:
        """DirectExecutor calls the LLM and returns structured output."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"severity": "high", "category": "security"}',
                "input_tokens": 100,
                "output_tokens": 50,
            },
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="Classify this alert by severity",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
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
        ))

        assert result.status == "completed"
        assert result.output == {"severity": "high", "category": "security"}
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self, mocker: MockerFixture) -> None:
        """DirectExecutor includes system prompt in messages."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Classify this alert",
            system_prompt="You are a security analyst.",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert any(m["role"] == "system" for m in messages)
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "security analyst" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_includes_context_in_prompt(self, mocker: MockerFixture) -> None:
        """DirectExecutor includes prior step context in the user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Based on the diagnosis, recommend a fix",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            context={
                "diagnosis": {
                    "status": "completed",
                    "output": {"severity": "high", "issue": "OOM"},
                },
            },
        ))

        user_msg = next(m for m in messages if m["role"] == "user")
        assert "OOM" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_plain_text_response_without_schema(self, mocker: MockerFixture) -> None:
        """DirectExecutor returns plain text wrapped in dict when no output_schema."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": "The alert is a false positive.",
                "input_tokens": 40,
                "output_tokens": 15,
            },
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="Summarize this alert",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.status == "completed"
        assert result.output == {"response": "The alert is a false positive."}

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self, mocker: MockerFixture) -> None:
        """DirectExecutor returns failed status on LLM error."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            side_effect=Exception("API rate limit exceeded"),
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="Classify this alert",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.status == "failed"
        assert "rate limit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_invalid_json_response_with_schema_fails(self, mocker: MockerFixture) -> None:
        """DirectExecutor fails when output_schema is set but LLM returns non-JSON."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": "Not valid JSON",
                "input_tokens": 30,
                "output_tokens": 10,
            },
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="Classify this",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            output_schema={"type": "object"},
        ))

        assert result.status == "failed"
        assert "non-json" in result.error.lower()

    @pytest.mark.asyncio
    async def test_transcript_records_llm_call(self, mocker: MockerFixture) -> None:
        """DirectExecutor records the LLM call in transcript."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"ok": true}',
                "input_tokens": 50,
                "output_tokens": 20,
            },
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="Check it",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            step_name="check",
        ))

        assert len(result.transcript) >= 1
        assert any(e.get("type") == "llm.call" for e in result.transcript)

    @pytest.mark.asyncio
    async def test_duration_tracked(self, mocker: MockerFixture) -> None:
        """DirectExecutor tracks execution duration."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"ok": true}',
                "input_tokens": 50,
                "output_tokens": 20,
            },
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="Check",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.duration_ms >= 0


class TestDirectExecutorCredentials:
    """Tests for credential resolution."""

    def test_resolves_credentials_from_env(self, mocker: MockerFixture) -> None:
        """_resolve_api_key resolves credentials_secret from environment."""
        from cloud_agents.workflow.executor.step.direct import _resolve_api_key

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-123"}, clear=False)

        key = _resolve_api_key({"name": "openai", "credentials_secret": "openai-api-key"})
        assert key == "sk-test-123"

    def test_resolves_uppercased_env_var(self, mocker: MockerFixture) -> None:
        """_resolve_api_key normalizes credential secret to UPPER_SNAKE."""
        from cloud_agents.workflow.executor.step.direct import _resolve_api_key

        mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-123"}, clear=False)

        key = _resolve_api_key({"name": "anthropic", "credentials_secret": "anthropic-api-key"})
        assert key == "sk-ant-123"

    def test_falls_back_to_provider_default(self, mocker: MockerFixture) -> None:
        """_resolve_api_key falls back to provider default env var."""
        from cloud_agents.workflow.executor.step.direct import _resolve_api_key

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-default"}, clear=False)

        key = _resolve_api_key({"name": "openai"})
        assert key == "sk-default"

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_failed(self, mocker: MockerFixture) -> None:
        """DirectExecutor fails gracefully when credentials are missing."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        env_copy = {k: v for k, v in os.environ.items() if "OPENAI" not in k and "ANTHROPIC" not in k}
        mocker.patch.dict(os.environ, env_copy, clear=True)

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        ))

        assert result.status == "failed"
        assert "api key" in result.error.lower()


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

        messages = _build_messages(StepInput(
            prompt="Hello",
            provider={"name": "openai", "model": "gpt-4o"},
        ))

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"

    def test_system_prompt_added_first(self) -> None:
        """System prompt appears before user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Hello",
            system_prompt="Be helpful",
            provider={"name": "openai", "model": "gpt-4o"},
        ))

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_output_schema_appended_to_user_message(self) -> None:
        """Output schema instructions appended to user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Classify",
            provider={"name": "openai", "model": "gpt-4o"},
            output_schema={"type": "object", "properties": {"severity": {"type": "string"}}},
        ))

        user_content = messages[-1]["content"]
        assert "JSON" in user_content
        assert "severity" in user_content

    def test_context_included_in_user_message(self) -> None:
        """Prior step outputs included in user message."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Fix the issue",
            provider={"name": "openai", "model": "gpt-4o"},
            context={
                "diagnosis": {
                    "status": "completed",
                    "output": {"issue": "disk full"},
                },
            },
        ))

        user_content = messages[-1]["content"]
        assert "disk full" in user_content

    def test_empty_context_not_added(self) -> None:
        """Empty context dict doesn't add context block."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Hello",
            provider={"name": "openai", "model": "gpt-4o"},
            context={},
        ))

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


class TestAntropicProviderRejection:
    """Tests for native Anthropic provider rejection."""

    @pytest.mark.asyncio
    async def test_anthropic_without_base_url_fails(self, mocker: MockerFixture) -> None:
        """DirectExecutor rejects native Anthropic provider without base_url."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False)

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "anthropic", "model": "claude-sonnet-5", "credentials_secret": "anthropic-api-key"},
        ))

        assert result.status == "failed"
        assert "non-openai-compatible" in result.error.lower()

    @pytest.mark.asyncio
    async def test_anthropic_with_base_url_allowed(self, mocker: MockerFixture) -> None:
        """Anthropic provider with explicit base_url (proxy) is allowed."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct._call_llm",
            return_value={
                "content": '{"ok": true}',
                "input_tokens": 10,
                "output_tokens": 5,
            },
        )

        executor = DirectExecutor()
        result = await executor.run(StepInput(
            prompt="test",
            provider={
                "name": "anthropic",
                "model": "claude-sonnet-5",
                "credentials_secret": "k",
                "base_url": "https://my-proxy.example.com/v1",
            },
        ))

        assert result.status == "completed"


class TestFalsyOutputPreservation:
    """Tests for preserving falsy prior-step outputs."""

    def test_empty_list_output_included(self) -> None:
        """Prior step output of [] is included in context."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Check results",
            provider={"name": "openai", "model": "gpt-4o"},
            context={
                "scan": {
                    "status": "completed",
                    "output": [],
                },
            },
        ))

        user_content = messages[-1]["content"]
        assert "Prior step" in user_content
        assert "[]" in user_content

    def test_zero_output_included(self) -> None:
        """Prior step output of 0 is included in context."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import _build_messages

        messages = _build_messages(StepInput(
            prompt="Check count",
            provider={"name": "openai", "model": "gpt-4o"},
            context={
                "count": {
                    "status": "completed",
                    "output": 0,
                },
            },
        ))

        user_content = messages[-1]["content"]
        assert "Prior step" in user_content
