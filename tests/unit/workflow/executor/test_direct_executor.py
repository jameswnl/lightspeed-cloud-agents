"""Tests for DirectExecutor — spawn: none LLM-only step executor."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic_ai.capabilities.instrumentation import Instrumentation
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

    def test_json_fence_stripped_with_schema(self) -> None:
        """```json ... ``` fenced JSON is parsed when output_schema is set.

        Regression test for #188 bug 1: gpt-4o-mini reliably wraps JSON
        answers in a markdown fence when only asked via prompt text.
        """
        from cloud_agents.workflow.executor.step.direct import _parse_output

        content = '```json\n{"severity": "high", "reason": "cpu spike"}\n```'
        result = _parse_output(content, {"type": "object"})
        assert result == {"severity": "high", "reason": "cpu spike"}

    def test_bare_fence_without_json_tag_stripped(self) -> None:
        """``` ... ``` (no "json" tag) is also stripped."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        content = '```\n{"ok": true}\n```'
        result = _parse_output(content, {"type": "object"})
        assert result == {"ok": True}

    def test_uppercase_json_tag_fence_stripped(self) -> None:
        """```JSON ... ``` (uppercase tag) is also stripped, not just lowercase.

        Regression test for a beesarmy review finding on #188 PR 190: the
        fence regex's "json" tag was lowercase-only, so a model emitting
        an uppercase ```JSON fence (the reported bug used lowercase, but
        the tag casing isn't part of any documented contract) would fail
        json.loads() on the still-fenced content instead of being
        stripped.
        """
        from cloud_agents.workflow.executor.step.direct import _parse_output

        content = '```JSON\n{"ok": true}\n```'
        result = _parse_output(content, {"type": "object"})
        assert result == {"ok": True}

    def test_fence_stripped_without_schema_too(self) -> None:
        """Fence-stripping also applies on the no-schema path."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        content = '```json\n{"ok": true}\n```'
        result = _parse_output(content, None)
        assert result == {"ok": True}

    def test_non_fenced_json_unaffected(self) -> None:
        """Plain (non-fenced) JSON still parses correctly."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        result = _parse_output('{"severity": "low"}', {"type": "object"})
        assert result == {"severity": "low"}

    def test_still_raises_for_genuinely_non_json_fenced_content(self) -> None:
        """A fence wrapping non-JSON text still raises -- stripping isn't a cure-all."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        with pytest.raises(ValueError, match="non-JSON"):
            _parse_output("```\nnot actually json\n```", {"type": "object"})

    def test_falls_back_to_original_content_on_unfenced_parse_failure(self) -> None:
        """No-schema path preserves the original (unstripped) text on parse failure."""
        from cloud_agents.workflow.executor.step.direct import _parse_output

        content = "```\nnot json at all\n```"
        result = _parse_output(content, None)
        assert result == {"response": content}


class TestCallLlmNativeStructuredOutput:
    """Tests for _call_llm's native structured-output attempt + fallback (#188).

    See tests/e2e/test_structured_output.py for the real-LLM regression
    test this complements -- these are fast/deterministic coverage of the
    specific request-shaping and fallback logic.
    """

    @pytest.mark.asyncio
    async def test_output_schema_triggers_native_mode_request(self, mocker: MockerFixture) -> None:
        """When output_schema is set, model_request is called with native output_mode."""
        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_fn = _mock_model_response(mocker, '{"ok": true}', 5, 5)

        await _call_llm(
            provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
            messages=[{"role": "user", "content": "hi"}],
            output_schema={"type": "object"},
        )

        mock_fn.assert_called_once()
        params = mock_fn.call_args.kwargs["model_request_parameters"]
        assert params.output_mode == "native"
        assert params.output_object.json_schema == {"type": "object"}

    @pytest.mark.asyncio
    async def test_no_output_schema_skips_native_mode(self, mocker: MockerFixture) -> None:
        """Without output_schema, model_request is called without model_request_parameters."""
        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_fn = _mock_model_response(mocker, "plain text", 5, 5)

        await _call_llm(
            provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
            messages=[{"role": "user", "content": "hi"}],
            output_schema=None,
        )

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("model_request_parameters") is None

    @pytest.mark.asyncio
    async def test_falls_back_when_native_mode_raises_user_error(
        self, mocker: MockerFixture
    ) -> None:
        """If native mode isn't supported (UserError), retries without it."""
        from pydantic_ai.exceptions import UserError
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=[UserError("native mode not supported"), mock_response],
        )

        result = await _call_llm(
            provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
            messages=[{"role": "user", "content": "hi"}],
            output_schema={"type": "object"},
        )

        assert mock_fn.call_count == 2
        assert mock_fn.call_args_list[0].kwargs.get("model_request_parameters") is not None
        assert mock_fn.call_args_list[1].kwargs.get("model_request_parameters") is None
        assert result["content"] == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_non_object_root_schema_skips_native_mode(self, mocker: MockerFixture) -> None:
        """A non-object-root output_schema (e.g. top-level array) skips native mode.

        Regression test for a CodeRabbit finding on #188 PR 190: OpenAI's
        Structured Outputs (native mode) requires an object-root JSON
        Schema. output_schema is user-authored workflow YAML, not
        internally guaranteed to be object-rooted -- passing e.g.
        {"type": "array", ...} through OutputObjectDefinition risks a
        provider-level rejection that the existing UserError-only fallback
        (see test_falls_back_when_native_mode_raises_user_error /
        test_non_user_error_propagates_without_fallback) would NOT catch,
        since that's an intentional "let real API errors propagate"
        design, not a place to add a second exception-based safety net.
        The fix instead avoids ever attempting native mode for a schema
        shape known not to support it.
        """
        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_fn = _mock_model_response(mocker, '["a", "b"]', 5, 5)

        result = await _call_llm(
            provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
            messages=[{"role": "user", "content": "hi"}],
            output_schema={"type": "array", "items": {"type": "string"}},
        )

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("model_request_parameters") is None
        assert result["content"] == '["a", "b"]'

    @pytest.mark.asyncio
    async def test_non_user_error_propagates_without_fallback(self, mocker: MockerFixture) -> None:
        """A non-UserError exception (e.g. a real API failure) propagates -- no silent retry."""
        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network exploded"),
        )

        with pytest.raises(RuntimeError, match="network exploded"):
            await _call_llm(
                provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
                messages=[{"role": "user", "content": "hi"}],
                output_schema={"type": "object"},
            )

        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_falls_back_on_400_model_http_error(self, mocker: MockerFixture) -> None:
        """A 400 ModelHTTPError (provider rejected the native schema) triggers fallback.

        Regression test for a beesarmy review finding on #188 PR 190:
        _supports_native_output() only screens out the *known* unsupported
        shape (non-object root) before ever attempting native mode -- an
        object-rooted schema can still be rejected by the provider for
        other reasons (unsupported keywords, draft mismatches, etc.),
        which surfaces as pydantic_ai.exceptions.ModelHTTPError, not
        UserError. A 400 specifically means "the request itself was
        invalid" (as opposed to 401/429/5xx, which are auth/rate-limit/
        infra failures unrelated to the schema) -- exactly the same class
        of "this schema doesn't work in native mode" signal UserError
        already triggers a fallback for.
        """
        from pydantic_ai.exceptions import ModelHTTPError
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=[
                ModelHTTPError(status_code=400, model_name="gpt-4o-mini", body="bad schema"),
                mock_response,
            ],
        )

        result = await _call_llm(
            provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
            messages=[{"role": "user", "content": "hi"}],
            output_schema={"type": "object"},
        )

        assert mock_fn.call_count == 2
        assert mock_fn.call_args_list[0].kwargs.get("model_request_parameters") is not None
        assert mock_fn.call_args_list[1].kwargs.get("model_request_parameters") is None
        assert result["content"] == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_5xx_model_http_error_propagates_without_fallback(
        self, mocker: MockerFixture
    ) -> None:
        """A 5xx ModelHTTPError (provider/infra failure, not a schema issue) propagates."""
        from pydantic_ai.exceptions import ModelHTTPError

        from cloud_agents.workflow.executor.step.direct import _call_llm

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=AsyncMock,
            side_effect=ModelHTTPError(status_code=503, model_name="gpt-4o-mini", body="down"),
        )

        with pytest.raises(ModelHTTPError):
            await _call_llm(
                provider={"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
                messages=[{"role": "user", "content": "hi"}],
                output_schema={"type": "object"},
            )

        assert mock_fn.call_count == 1


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
                allowed_skills=["k8s-diag"],
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
                allowed_skills=["k8s-diag"],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "capabilities" in call_kwargs
        assert mock_cap in call_kwargs["capabilities"]
        assert any(isinstance(c, Instrumentation) for c in call_kwargs["capabilities"])

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
                allowed_skills=["k8s-diag"],
            )
        )

        assert result.status == "completed"
        mock_agent_cls.assert_called_once()
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "capabilities" in call_kwargs
        assert mock_cap in call_kwargs["capabilities"]
        assert any(isinstance(c, Instrumentation) for c in call_kwargs["capabilities"])

    @pytest.mark.asyncio
    async def test_agent_only_instrumentation_capability_when_skills_path_unset(
        self, mocker: MockerFixture
    ) -> None:
        """Agent has only the Instrumentation capability when CLOUD_AGENTS_SKILLS_PATHS is unset."""
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
                allowed_skills=["k8s-diag"],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        capabilities = call_kwargs.get("capabilities")
        assert capabilities is not None
        assert len(capabilities) == 1
        assert isinstance(capabilities[0], Instrumentation)

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
                allowed_skills=["k8s-diag"],
            )
        )

        call_kwargs = mock_agent_cls.call_args.kwargs
        # All three: tools, toolsets, and capabilities
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) == 1
        assert "toolsets" in call_kwargs
        assert len(call_kwargs["toolsets"]) == 1
        assert "capabilities" in call_kwargs
        assert mock_cap in call_kwargs["capabilities"]
        assert any(isinstance(c, Instrumentation) for c in call_kwargs["capabilities"])


class TestDirectExecutorInstrumentation:
    """spawn: none emits pydantic-ai agent/model spans nested under step.execute (issue #263)."""

    @pytest.mark.asyncio
    async def test_model_request_path_is_instrumented(self, mocker: MockerFixture) -> None:
        """The plain model_request call (_call_llm) is instrumented."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch("cloud_agents.workflow.executor.step.direct.ensure_credentials_env")
        mock_fn = _mock_model_response(mocker, text='{"ok": true}', input_tokens=1, output_tokens=1)

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("instrument") is True

    @pytest.mark.asyncio
    async def test_agent_run_path_is_instrumented(self, mocker: MockerFixture) -> None:
        """The Agent (tools/MCP/skills) path gets the Instrumentation capability."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch("cloud_agents.workflow.executor.step.direct.ensure_credentials_env")

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch("cloud_agents.workflow.executor.step.direct.Agent")
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

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert any(isinstance(c, Instrumentation) for c in call_kwargs["capabilities"])


class TestDirectExecutorAllowedSkillsDefaults:
    """Least-privilege defaults for allowed_skills (issue #202)."""

    @pytest.mark.asyncio
    async def test_omitted_allowed_skills_does_not_call_get_skills(self, mocker: MockerFixture) -> None:
        """Omitted allowed_skills -> get_skills_capability not called, even with env set."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch("cloud_agents.workflow.executor.step.direct.ensure_credentials_env")
        mock_agent_cls = mocker.patch("cloud_agents.workflow.executor.step.direct.Agent")
        mock_agent_instance = mocker.AsyncMock()
        mock_result = mocker.MagicMock()
        mock_result.output = "{\"response\": \"hi\"}"
        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 5
        mock_usage.output_tokens = 5
        mock_result.usage = mock_usage
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_get_skills = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability"
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="hi",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        mock_get_skills.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowed_skills_no_capability(self, mocker: MockerFixture) -> None:
        """allowed_skills=[] -> no capability (include=[] short-circuits to None)."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch("cloud_agents.workflow.executor.step.direct.ensure_credentials_env")
        mock_caps = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability",
            return_value=None,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.model_request",
            new_callable=mocker.AsyncMock,
        )

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="hi",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                allowed_skills=[],
            )
        )

        # Empty list returns None via skills.py guard -> no capability -> falls back to model_request path
        mock_caps.assert_called_once_with(include=[])
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_allowed_skills_forwards_include(self, mocker: MockerFixture) -> None:
        """allowed_skills=[...] -> get_skills_capability(include=[...]) asserted on mock args."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch("cloud_agents.workflow.executor.step.direct.ensure_credentials_env")
        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_result = mocker.MagicMock()
        mock_result.output = "{\"ok\": true}"
        mock_result.usage = mock_usage
        mock_agent_cls = mocker.patch("cloud_agents.workflow.executor.step.direct.Agent")
        mock_agent_instance = mocker.AsyncMock()
        mock_agent_instance.run.return_value = mock_result
        mock_agent_cls.return_value = mock_agent_instance

        mock_cap = mocker.MagicMock()
        mock_get_skills = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.get_skills_capability",
            return_value=mock_cap,
        )

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                allowed_skills=["k8s-diag"],
            )
        )

        mock_get_skills.assert_called_once_with(include=["k8s-diag"])
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert mock_cap in call_kwargs["capabilities"]
        assert any(isinstance(c, Instrumentation) for c in call_kwargs["capabilities"])


class TestMessageHistory:
    """Tests for Fix 2: message_history from conversation context."""

    @pytest.mark.asyncio
    async def test_conversation_context_routes_to_agent(self, mocker: MockerFixture) -> None:
        """Context with conversation messages routes to Agent path even without tools."""
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

        # Context with conversation messages (output.messages structure)
        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi there!"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Follow up question",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
                tools=[],
            )
        )

        assert result.status == "completed"
        # Agent should have been used (not model_request)
        mock_agent_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_message_history_passed_to_agent(self, mocker: MockerFixture) -> None:
        """Agent.run() receives message_history from conversation context."""
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

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi there!"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Follow up question",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        )

        # Verify message_history was passed to agent.run()
        run_kwargs = mock_agent_instance.run.call_args.kwargs
        assert "message_history" in run_kwargs
        assert run_kwargs["message_history"] is not None
        assert len(run_kwargs["message_history"]) == 2

    @pytest.mark.asyncio
    async def test_message_history_correct_roles(self, mocker: MockerFixture) -> None:
        """message_history has correct ModelRequest/ModelResponse types."""
        from pydantic_ai.messages import ModelRequest, ModelResponse

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

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Next question",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        )

        run_kwargs = mock_agent_instance.run.call_args.kwargs
        history = run_kwargs["message_history"]
        assert isinstance(history[0], ModelRequest)
        assert isinstance(history[1], ModelResponse)

    @pytest.mark.asyncio
    async def test_no_conversation_context_no_message_history(
        self, mocker: MockerFixture
    ) -> None:
        """Without conversation context, no message_history is passed."""
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

        # Non-conversation context (regular step outputs)
        context = {
            "diagnosis": {
                "status": "completed",
                "output": {"severity": "high", "issue": "OOM"},
            },
        }

        executor = DirectExecutor()
        result = await executor.run(
            StepInput(
                prompt="Fix the issue",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        )

        assert result.status == "completed"
        # Should have used model_request, not Agent
        mock_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_multi_turn_message_history_ordering(self, mocker: MockerFixture) -> None:
        """message_history from multiple turns is in chronological order."""
        from pydantic_ai.messages import ModelRequest, ModelResponse

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

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "First"},
                        {"role": "assistant", "content": "Reply 1"},
                    ]
                },
            },
            "turn-1": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Second"},
                        {"role": "assistant", "content": "Reply 2"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Third question",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        )

        run_kwargs = mock_agent_instance.run.call_args.kwargs
        history = run_kwargs["message_history"]
        assert len(history) == 4

        # Verify order: turn-0 messages before turn-1 messages
        assert isinstance(history[0], ModelRequest)
        assert isinstance(history[1], ModelResponse)
        assert isinstance(history[2], ModelRequest)
        assert isinstance(history[3], ModelResponse)

    @pytest.mark.asyncio
    async def test_conversation_context_excludes_non_conversation_from_history(
        self, mocker: MockerFixture
    ) -> None:
        """Non-conversation context entries are not added to message_history."""
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

        # Mix of conversation and non-conversation context
        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                    ]
                },
            },
            "diagnosis": {
                "status": "completed",
                "output": {"severity": "high"},
            },
        }

        executor = DirectExecutor()
        await executor.run(
            StepInput(
                prompt="Follow up",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        )

        run_kwargs = mock_agent_instance.run.call_args.kwargs
        history = run_kwargs["message_history"]
        # Only the conversation messages, not the diagnosis
        assert len(history) == 2


class TestRunStreamWithMessageHistory:
    """Tests for Fix 1: run_stream() needs message_history for conversation context."""

    @pytest.mark.asyncio
    async def test_run_stream_with_conversation_context_uses_agent_path(
        self, mocker: MockerFixture
    ) -> None:
        """run_stream() uses Agent path when conversation context is present."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_streamed = mocker.MagicMock()
        mock_streamed.stream_text = mocker.MagicMock(
            return_value=_async_iter(["Hello", " there"])
        )
        mock_streamed.get_output = mocker.AsyncMock(return_value="Hello there")
        mock_streamed.usage = mock_usage
        mock_streamed.__aenter__ = mocker.AsyncMock(return_value=mock_streamed)
        mock_streamed.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(return_value=mock_streamed)
        mock_agent_cls.return_value = mock_agent_instance

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi there!"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        events: list[StreamEvent] = []
        async for event in executor.run_stream(
            StepInput(
                prompt="Follow up question",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
                tools=[],
            )
        ):
            events.append(event)

        # Should have used Agent streaming, not super().run_stream fallback
        token_events = [e for e in events if e.type == "token"]
        complete_events = [e for e in events if e.type == "complete"]
        assert len(token_events) == 2
        assert len(complete_events) == 1
        mock_agent_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_stream_passes_message_history_to_agent(
        self, mocker: MockerFixture
    ) -> None:
        """run_stream() passes message_history to agent.run_stream()."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_streamed = mocker.MagicMock()
        mock_streamed.stream_text = mocker.MagicMock(return_value=_async_iter(["ok"]))
        mock_streamed.get_output = mocker.AsyncMock(return_value="ok")
        mock_streamed.usage = mock_usage
        mock_streamed.__aenter__ = mocker.AsyncMock(return_value=mock_streamed)
        mock_streamed.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(return_value=mock_streamed)
        mock_agent_cls.return_value = mock_agent_instance

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        async for _ in executor.run_stream(
            StepInput(
                prompt="Follow up",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        ):
            pass

        # Verify message_history was passed to agent.run_stream()
        run_stream_kwargs = mock_agent_instance.run_stream.call_args.kwargs
        assert "message_history" in run_stream_kwargs
        assert run_stream_kwargs["message_history"] is not None
        assert len(run_stream_kwargs["message_history"]) == 2

    @pytest.mark.asyncio
    async def test_run_stream_conversation_prompt_no_prior_step_outputs(
        self, mocker: MockerFixture
    ) -> None:
        """run_stream() with conversation context does not flatten context into prompt."""
        from cloud_agents.workflow.executor.step.base import StepInput, StreamEvent
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_streamed = mocker.MagicMock()
        mock_streamed.stream_text = mocker.MagicMock(return_value=_async_iter(["ok"]))
        mock_streamed.get_output = mocker.AsyncMock(return_value="ok")
        mock_streamed.usage = mock_usage
        mock_streamed.__aenter__ = mocker.AsyncMock(return_value=mock_streamed)
        mock_streamed.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.direct.Agent",
        )
        mock_agent_instance = mocker.MagicMock()
        mock_agent_instance.run_stream = mocker.MagicMock(return_value=mock_streamed)
        mock_agent_cls.return_value = mock_agent_instance

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                    ]
                },
            },
        }

        executor = DirectExecutor()
        async for _ in executor.run_stream(
            StepInput(
                prompt="Follow up question",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context=context,
            )
        ):
            pass

        # The user prompt passed to agent.run_stream() should NOT contain
        # "Prior step outputs" -- context is in message_history
        run_stream_args = mock_agent_instance.run_stream.call_args
        user_prompt = run_stream_args[0][0]
        assert "Prior step outputs" not in user_prompt
        assert user_prompt == "Follow up question"


class TestBuildMessageHistoryToolRoles:
    """Tests for _build_message_history tool role handling.

    Updated in #158: tool_call/tool_result are now replayed as proper
    pydantic-ai ToolCallPart/ToolReturnPart (previously skipped).
    """

    def test_tool_call_role_replayed(self) -> None:
        """tool_call messages are replayed as ToolCallPart in ModelResponse."""
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Run kubectl get pods"},
                        {"role": "tool_call", "content": ""},
                        {"role": "tool_result", "content": "pod-1 Running"},
                        {"role": "assistant", "content": "The pods are running."},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        # user, tool_call(ModelResponse), tool_result(ModelRequest), assistant
        assert len(history) == 4
        assert isinstance(history[1], ModelResponse)
        assert isinstance(history[1].parts[0], ToolCallPart)

    def test_tool_result_role_replayed(self) -> None:
        """tool_result messages are replayed as ToolReturnPart in ModelRequest."""
        from pydantic_ai.messages import ModelRequest, ToolReturnPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool_call", "content": ""},
                        {"role": "tool_result", "content": "some result"},
                        {"role": "assistant", "content": "Got it."},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        assert len(history) == 4
        assert isinstance(history[2], ModelRequest)
        tool_return_parts = [
            p for p in history[2].parts if isinstance(p, ToolReturnPart)
        ]
        assert len(tool_return_parts) == 1
        assert tool_return_parts[0].content == "some result"

    def test_tool_roles_only_no_crash(self) -> None:
        """Messages with only tool roles produce valid history without crashing."""
        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "tool_call", "content": "some_tool()"},
                        {"role": "tool_result", "content": "result"},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        # Should produce a ModelResponse (tool_call) and ModelRequest (tool_result)
        assert len(history) == 2

    def test_unknown_role_skipped(self) -> None:
        """Unknown roles are skipped without crashing."""
        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "system", "content": "System message"},
                        {"role": "assistant", "content": "Hi!"},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        # Only user and assistant (system is unknown, skipped)
        assert len(history) == 2


class TestBuildMessageHistoryToolReplay:
    """Tests for _build_message_history() replaying tool_call/tool_result (#158)."""

    def test_tool_call_creates_tool_call_part_in_model_response(self) -> None:
        """tool_call role creates ToolCallPart inside a ModelResponse."""
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Get pods"},
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "kubectl_get",
                                "args": {"namespace": "default"},
                            },
                        },
                        {
                            "role": "tool_result",
                            "content": "pod-1 Running",
                            "metadata": {"tool_name": "kubectl_get"},
                        },
                        {"role": "assistant", "content": "Pods are running."},
                    ]
                },
            },
        }

        history = _build_message_history(context)

        # Should have: ModelRequest(user), ModelResponse(tool_call),
        # ModelRequest(tool_return), ModelResponse(assistant)
        assert len(history) == 4

        # Second entry should be a ModelResponse with ToolCallPart
        assert isinstance(history[1], ModelResponse)
        assert len(history[1].parts) == 1
        assert isinstance(history[1].parts[0], ToolCallPart)
        assert history[1].parts[0].tool_name == "kubectl_get"

    def test_tool_result_creates_tool_return_part_in_model_request(self) -> None:
        """tool_result role creates ToolReturnPart inside a ModelRequest."""
        from pydantic_ai.messages import ModelRequest, ToolReturnPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Get pods"},
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "kubectl_get",
                                "args": {"namespace": "default"},
                            },
                        },
                        {
                            "role": "tool_result",
                            "content": "pod-1 Running",
                            "metadata": {"tool_name": "kubectl_get"},
                        },
                        {"role": "assistant", "content": "Pods are running."},
                    ]
                },
            },
        }

        history = _build_message_history(context)

        # Third entry should be a ModelRequest with ToolReturnPart
        assert isinstance(history[2], ModelRequest)
        tool_return_parts = [
            p for p in history[2].parts if isinstance(p, ToolReturnPart)
        ]
        assert len(tool_return_parts) == 1
        assert tool_return_parts[0].tool_name == "kubectl_get"
        assert tool_return_parts[0].content == "pod-1 Running"

    def test_full_conversation_with_tools_ordering(self) -> None:
        """Full conversation: user -> tool_call -> tool_result -> assistant."""
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            ToolCallPart,
            ToolReturnPart,
        )

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "List pods"},
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "kubectl_get",
                                "args": {"resource": "pods"},
                            },
                        },
                        {
                            "role": "tool_result",
                            "content": "pod-1 Running",
                            "metadata": {"tool_name": "kubectl_get"},
                        },
                        {"role": "assistant", "content": "Here are the pods."},
                    ]
                },
            },
            "turn-1": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Delete pod-1"},
                        {"role": "assistant", "content": "Deleted pod-1."},
                    ]
                },
            },
        }

        history = _build_message_history(context)

        # turn-0: user, tool_call(in ModelResponse), tool_result(in ModelRequest), assistant
        # turn-1: user, assistant
        assert len(history) == 6

        assert isinstance(history[0], ModelRequest)  # user
        assert isinstance(history[1], ModelResponse)  # tool_call
        assert isinstance(history[1].parts[0], ToolCallPart)
        assert isinstance(history[2], ModelRequest)  # tool_result
        tool_return_parts = [
            p for p in history[2].parts if isinstance(p, ToolReturnPart)
        ]
        assert len(tool_return_parts) == 1
        assert isinstance(history[3], ModelResponse)  # assistant
        assert isinstance(history[4], ModelRequest)  # user (turn-1)
        assert isinstance(history[5], ModelResponse)  # assistant (turn-1)

    def test_consecutive_tool_calls_appended_to_same_response(self) -> None:
        """Multiple consecutive tool_calls are appended to the same ModelResponse."""
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Check cluster"},
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "kubectl_get",
                                "args": {"resource": "pods"},
                            },
                        },
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "kubectl_get",
                                "args": {"resource": "services"},
                            },
                        },
                        {
                            "role": "tool_result",
                            "content": "pod-1 Running",
                            "metadata": {"tool_name": "kubectl_get"},
                        },
                        {
                            "role": "tool_result",
                            "content": "svc-1 ClusterIP",
                            "metadata": {"tool_name": "kubectl_get"},
                        },
                        {"role": "assistant", "content": "Cluster is healthy."},
                    ]
                },
            },
        }

        history = _build_message_history(context)

        # user, tool_calls(in one ModelResponse), 2x tool_result(in ModelRequests), assistant
        # The two consecutive tool_calls should be in a single ModelResponse
        tool_call_responses = [
            h
            for h in history
            if isinstance(h, ModelResponse)
            and any(isinstance(p, ToolCallPart) for p in h.parts)
        ]
        assert len(tool_call_responses) == 1
        assert len(tool_call_responses[0].parts) == 2

    def test_tool_call_args_passed_directly(self) -> None:
        """tool_call args are passed directly to ToolCallPart (dict or str)."""
        from pydantic_ai.messages import ToolCallPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Get pods"},
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "kubectl_get",
                                "args": {"namespace": "default"},
                            },
                        },
                        {
                            "role": "tool_result",
                            "content": "ok",
                            "metadata": {"tool_name": "kubectl_get"},
                        },
                        {"role": "assistant", "content": "Done."},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        tcp = history[1].parts[0]
        assert isinstance(tcp, ToolCallPart)
        assert tcp.args == {"namespace": "default"}

    def test_tool_call_id_uses_synthetic_id(self) -> None:
        """Tool call IDs use synthetic 'call_{tool_name}' format."""
        from pydantic_ai.messages import ToolCallPart, ToolReturnPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Read file"},
                        {
                            "role": "tool_call",
                            "content": "",
                            "metadata": {
                                "tool_name": "read_file",
                                "args": {"path": "/tmp/test"},
                            },
                        },
                        {
                            "role": "tool_result",
                            "content": "file contents",
                            "metadata": {"tool_name": "read_file"},
                        },
                        {"role": "assistant", "content": "Here's the file."},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        tcp = history[1].parts[0]
        assert isinstance(tcp, ToolCallPart)
        assert tcp.tool_call_id.startswith("call_read_file")

        trp_parts = [
            p for p in history[2].parts if isinstance(p, ToolReturnPart)
        ]
        assert trp_parts[0].tool_call_id.startswith("call_read_file")

    def test_user_assistant_only_backward_compatible(self) -> None:
        """Conversations without tool roles still work (backward compat)."""
        from pydantic_ai.messages import ModelRequest, ModelResponse

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        assert len(history) == 2
        assert isinstance(history[0], ModelRequest)
        assert isinstance(history[1], ModelResponse)

    def test_missing_metadata_handled_gracefully(self) -> None:
        """tool_call with missing metadata does not crash."""
        from pydantic_ai.messages import ToolCallPart

        from cloud_agents.workflow.executor.step.direct import _build_message_history

        context = {
            "turn-0": {
                "status": "completed",
                "output": {
                    "messages": [
                        {"role": "user", "content": "Test"},
                        {
                            "role": "tool_call",
                            "content": "",
                        },
                        {
                            "role": "tool_result",
                            "content": "result",
                        },
                        {"role": "assistant", "content": "Done."},
                    ]
                },
            },
        }

        history = _build_message_history(context)
        # Should not crash, should produce entries with defaults
        assert len(history) == 4
        tcp = history[1].parts[0]
        assert isinstance(tcp, ToolCallPart)
        assert tcp.tool_name == ""
