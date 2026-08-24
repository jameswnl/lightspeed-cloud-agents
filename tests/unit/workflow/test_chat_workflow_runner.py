"""Tests for ChatWorkflowRunner implementing WorkflowRunner interface."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from cloud_agents.workflow.executor.base import WorkflowRunner
from cloud_agents.workflow.executor.chat.runner import (
    ChatWorkflowConfig,
    ChatWorkflowRunner,
)
from cloud_agents.workflow.executor.step.base import StepResult, StreamEvent

_PROVIDER = {
    "name": "openai",
    "model": "gpt-4o",
    "credentials_secret": "openai-api-key",
}


@pytest.fixture(name="mock_run_store")
def mock_run_store_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock RunStateStore."""
    store = mocker.AsyncMock()
    store.create = mocker.AsyncMock()
    store.get = mocker.AsyncMock(return_value=None)
    store.update_step = mocker.AsyncMock()
    store.append_event = mocker.AsyncMock()
    store.mark_terminal = mocker.AsyncMock()
    store.list_paused = mocker.AsyncMock(return_value=[])
    return store


@pytest.fixture(name="mock_transcript_store")
def mock_transcript_store_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock TranscriptStore."""
    store = mocker.AsyncMock()
    store.save = mocker.AsyncMock()
    store.load_recent_turns = mocker.AsyncMock(return_value=[])
    store.list_steps = mocker.AsyncMock(return_value=[])
    store.get = mocker.AsyncMock(return_value=None)
    return store


@pytest.fixture(name="config")
def config_fixture() -> ChatWorkflowConfig:
    """Default chat config."""
    return ChatWorkflowConfig(
        provider=_PROVIDER,
        system_prompt="You are a helpful assistant.",
        max_context_turns=20,
    )


@pytest.fixture(name="runner")
def runner_fixture(
    mock_run_store: AsyncMock,
    mock_transcript_store: AsyncMock,
    config: ChatWorkflowConfig,
) -> ChatWorkflowRunner:
    """Create a ChatWorkflowRunner with mocked dependencies."""
    return ChatWorkflowRunner(
        run_store=mock_run_store,
        transcript_store=mock_transcript_store,
        config=config,
    )


class TestChatWorkflowConfig:
    """Tests for ChatWorkflowConfig dataclass."""

    def test_defaults(self) -> None:
        """Config has sensible defaults."""
        cfg = ChatWorkflowConfig(provider=_PROVIDER)
        assert cfg.system_prompt is None
        assert cfg.tools == []
        assert cfg.tools_module is None
        assert cfg.mcp_servers is None
        assert cfg.max_context_turns == 20
        assert cfg.spawn == "none"

    def test_custom_values(self) -> None:
        """Config accepts custom values."""
        cfg = ChatWorkflowConfig(
            provider=_PROVIDER,
            system_prompt="Custom prompt",
            tools=["kubectl", "read_file"],
            tools_module="my.tools",
            mcp_servers=[{"name": "test", "url": "http://localhost:8080"}],
            max_context_turns=10,
            spawn="local",
        )
        assert cfg.system_prompt == "Custom prompt"
        assert cfg.tools == ["kubectl", "read_file"]
        assert cfg.max_context_turns == 10
        assert cfg.spawn == "local"


class TestImplementsABC:
    """Verify ChatWorkflowRunner implements WorkflowRunner ABC."""

    def test_isinstance_check(self, runner: ChatWorkflowRunner) -> None:
        """ChatWorkflowRunner is an instance of WorkflowRunner."""
        assert isinstance(runner, WorkflowRunner)


class TestStart:
    """Tests for start() -- create a new conversation."""

    @pytest.mark.asyncio
    async def test_start_creates_conversation(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """start() creates a run in the state store and returns workflow_id."""
        wf_id = await runner.start({"user_id": "u1"})

        assert wf_id.startswith("chat-")
        mock_run_store.create.assert_called_once()
        call_kwargs = mock_run_store.create.call_args
        assert call_kwargs.kwargs["workflow_id"] == wf_id
        assert call_kwargs.kwargs["workflow_name"] == "chat"

    @pytest.mark.asyncio
    async def test_start_with_custom_workflow_id(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """start() uses provided workflow_id if given."""
        wf_id = await runner.start({"workflow_id": "my-conv-1", "user_id": "u1"})

        assert wf_id == "my-conv-1"
        call_kwargs = mock_run_store.create.call_args
        assert call_kwargs.kwargs["workflow_id"] == "my-conv-1"

    @pytest.mark.asyncio
    async def test_start_passes_user_and_session(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """start() passes user_id and session_id to the store."""
        await runner.start({"user_id": "u1", "session_id": "s1"})

        call_kwargs = mock_run_store.create.call_args
        assert call_kwargs.kwargs["user_id"] == "u1"
        assert call_kwargs.kwargs["session_id"] == "s1"


class TestSendMessage:
    """Tests for send_message() -- execute one turn."""

    @pytest.mark.asyncio
    async def test_send_message_returns_step_result(
        self, runner: ChatWorkflowRunner, mocker: MockerFixture
    ) -> None:
        """send_message() executes via step executor and returns StepResult."""
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"response": "Hello!"},
            input_tokens=10,
            output_tokens=5,
            duration_ms=100,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        result = await runner.send_message("chat-123", "Hi there")

        assert result.status == "completed"
        assert result.output == {"response": "Hello!"}

    @pytest.mark.asyncio
    async def test_send_message_saves_transcript(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message() saves the turn to the transcript store."""
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"response": "Hello!"},
            input_tokens=10,
            output_tokens=5,
            duration_ms=100,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        await runner.send_message("chat-123", "Hi there")

        mock_transcript_store.save.assert_called_once()
        call_kwargs = mock_transcript_store.save.call_args
        assert call_kwargs.kwargs["workflow_id"] == "chat-123"
        assert call_kwargs.kwargs["step_name"] == "turn-0"
        # Verify messages include both user and assistant
        messages = call_kwargs.kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hi there"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_send_message_loads_prior_turns(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message() loads prior turns from transcript store."""
        mock_transcript_store.load_recent_turns.return_value = [
            {
                "step_name": "turn-0",
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi!"},
                ],
            },
        ]
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"response": "I'm doing well!"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        await runner.send_message("chat-123", "How are you?")

        mock_transcript_store.load_recent_turns.assert_called_once_with("chat-123", limit=20)
        # Verify context was passed to StepInput
        call_args = mock_executor.run.call_args[0][0]
        assert "turn-0" in call_args.context

    @pytest.mark.asyncio
    async def test_send_message_increments_turn_name(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message() increments turn name based on prior turns."""
        mock_transcript_store.load_recent_turns.return_value = [
            {"step_name": "turn-0", "messages": [{"role": "user", "content": "a"}]},
            {"step_name": "turn-1", "messages": [{"role": "user", "content": "b"}]},
        ]
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"response": "c"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        await runner.send_message("chat-123", "third message")

        save_kwargs = mock_transcript_store.save.call_args.kwargs
        assert save_kwargs["step_name"] == "turn-2"

    @pytest.mark.asyncio
    async def test_send_message_updates_run_state(
        self,
        runner: ChatWorkflowRunner,
        mock_run_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message() updates step in run state store."""
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"response": "OK"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        await runner.send_message("chat-123", "test")

        mock_run_store.update_step.assert_called_once_with(
            "chat-123",
            "turn-0",
            "completed",
            output={"response": "OK"},
            error=None,
        )

    @pytest.mark.asyncio
    async def test_max_context_turns_limits_history(
        self,
        mock_run_store: AsyncMock,
        mock_transcript_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message() respects max_context_turns config."""
        config = ChatWorkflowConfig(provider=_PROVIDER, max_context_turns=5)
        small_runner = ChatWorkflowRunner(
            run_store=mock_run_store,
            transcript_store=mock_transcript_store,
            config=config,
        )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(status="completed", output={"response": "ok"})
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        await small_runner.send_message("chat-123", "test")

        mock_transcript_store.load_recent_turns.assert_called_once_with("chat-123", limit=5)

    @pytest.mark.asyncio
    async def test_send_message_failed_result(
        self,
        runner: ChatWorkflowRunner,
        mock_run_store: AsyncMock,
        mock_transcript_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message() handles failed step results correctly."""
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="failed",
            error="LLM timeout",
        )
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        result = await runner.send_message("chat-123", "test")

        assert result.status == "failed"
        assert result.error == "LLM timeout"
        mock_run_store.update_step.assert_called_once_with(
            "chat-123",
            "turn-0",
            "failed",
            output=None,
            error="LLM timeout",
        )


class TestSendMessageStream:
    """Tests for send_message_stream() -- streaming variant."""

    @pytest.mark.asyncio
    async def test_send_message_stream_yields_events(
        self,
        runner: ChatWorkflowRunner,
        mocker: MockerFixture,
    ) -> None:
        """send_message_stream() yields StreamEvents and saves transcript."""
        step_result = StepResult(
            status="completed",
            output={"response": "Hello!"},
            input_tokens=10,
            output_tokens=5,
            duration_ms=100,
        )

        async def mock_run_stream(step_input: Any) -> Any:
            yield StreamEvent(type="token", data={"delta": "Hello"})
            yield StreamEvent(type="token", data={"delta": "!"})
            yield StreamEvent(type="complete", result=step_result)

        mock_executor = mocker.AsyncMock()
        mock_executor.run_stream = mock_run_stream
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        events = []
        async for event in runner.send_message_stream("chat-123", "Hi"):
            events.append(event)

        assert len(events) == 3
        assert events[0].type == "token"
        assert events[1].type == "token"
        assert events[2].type == "complete"
        assert events[2].result is not None
        assert events[2].result.status == "completed"

    @pytest.mark.asyncio
    async def test_send_message_stream_saves_transcript(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """send_message_stream() saves the turn to the transcript store after completion."""
        step_result = StepResult(
            status="completed",
            output={"response": "Done!"},
            input_tokens=5,
            output_tokens=3,
            duration_ms=50,
        )

        async def mock_run_stream(step_input: Any) -> Any:
            yield StreamEvent(type="complete", result=step_result)

        mock_executor = mocker.AsyncMock()
        mock_executor.run_stream = mock_run_stream
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        async for _ in runner.send_message_stream("chat-123", "test"):
            pass

        mock_transcript_store.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_stream_error_event(
        self,
        runner: ChatWorkflowRunner,
        mocker: MockerFixture,
    ) -> None:
        """send_message_stream() handles error events from executor."""
        error_result = StepResult(status="failed", error="connection lost")

        async def mock_run_stream(step_input: Any) -> Any:
            yield StreamEvent(
                type="error",
                data={"error": "connection lost"},
                result=error_result,
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run_stream = mock_run_stream
        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            return_value=mock_executor,
        )

        events = []
        async for event in runner.send_message_stream("chat-123", "test"):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"


class TestGetHistory:
    """Tests for get_history() -- load conversation messages."""

    @pytest.mark.asyncio
    async def test_get_history_returns_messages(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
    ) -> None:
        """get_history() loads and converts ConversationMessages."""
        mock_transcript_store.load_recent_turns.return_value = [
            {
                "step_name": "turn-0",
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi!"},
                ],
            },
            {
                "step_name": "turn-1",
                "messages": [
                    {"role": "user", "content": "How are you?"},
                    {"role": "assistant", "content": "Good!"},
                ],
            },
        ]

        messages = await runner.get_history("chat-123")

        assert len(messages) == 4
        assert messages[0].role == "user"
        assert messages[0].content == "Hello"
        assert messages[1].role == "assistant"
        assert messages[1].content == "Hi!"
        assert messages[2].role == "user"
        assert messages[2].content == "How are you?"
        assert messages[3].role == "assistant"
        assert messages[3].content == "Good!"

    @pytest.mark.asyncio
    async def test_get_history_empty(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
    ) -> None:
        """get_history() returns empty list when no turns exist."""
        mock_transcript_store.load_recent_turns.return_value = []

        messages = await runner.get_history("chat-123")
        assert messages == []

    @pytest.mark.asyncio
    async def test_get_history_respects_limit(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
    ) -> None:
        """get_history() passes limit to transcript store."""
        mock_transcript_store.load_recent_turns.return_value = []

        await runner.get_history("chat-123", limit=5)

        mock_transcript_store.load_recent_turns.assert_called_once_with("chat-123", limit=5)


class TestGetStatus:
    """Tests for get_status() -- delegates to run state store."""

    @pytest.mark.asyncio
    async def test_get_status_delegates_to_store(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """get_status() returns WorkflowStatus from run state store."""
        mock_run_store.get.return_value = {
            "workflow_id": "chat-123",
            "workflow_name": "chat",
            "status": "running",
            "current_step": None,
            "steps": {},
            "events": [],
            "definition": {},
            "provider": {},
            "authz_context": {},
            "workflow_context": {},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }

        status = await runner.get_status("chat-123")

        assert status.workflow_id == "chat-123"
        assert status.status == "running"
        assert status.is_terminal is False

    @pytest.mark.asyncio
    async def test_get_status_not_found_raises(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """get_status() raises KeyError for nonexistent workflow."""
        mock_run_store.get.return_value = None

        with pytest.raises(KeyError):
            await runner.get_status("chat-nonexistent")


class TestCancel:
    """Tests for cancel() -- mark as completed."""

    @pytest.mark.asyncio
    async def test_cancel_marks_completed(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """cancel() marks the workflow as cancelled."""
        mock_run_store.get.return_value = {"status": "running"}

        await runner.cancel("chat-123")

        mock_run_store.mark_terminal.assert_called_once_with("chat-123", "cancelled")

    @pytest.mark.asyncio
    async def test_cancel_not_found_raises(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """cancel() raises KeyError for nonexistent workflow."""
        mock_run_store.get.return_value = None

        with pytest.raises(KeyError):
            await runner.cancel("chat-nonexistent")


class TestIsTerminal:
    """Tests for is_terminal() -- delegates to store."""

    @pytest.mark.asyncio
    async def test_completed_is_terminal(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """Completed workflow is terminal."""
        mock_run_store.get.return_value = {"status": "completed"}
        assert await runner.is_terminal("chat-123") is True

    @pytest.mark.asyncio
    async def test_running_is_not_terminal(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """Running workflow is not terminal."""
        mock_run_store.get.return_value = {"status": "running"}
        assert await runner.is_terminal("chat-123") is False

    @pytest.mark.asyncio
    async def test_cancelled_is_terminal(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """Cancelled workflow is terminal."""
        mock_run_store.get.return_value = {"status": "cancelled"}
        assert await runner.is_terminal("chat-123") is True


class TestApprove:
    """Tests for approve() -- not supported in chat mode."""

    @pytest.mark.asyncio
    async def test_approve_raises_not_implemented(self, runner: ChatWorkflowRunner) -> None:
        """approve() raises NotImplementedError for chat mode."""
        from cloud_agents.workflow.executor.base import ApprovalDecision

        with pytest.raises(NotImplementedError, match="does not support approval"):
            await runner.approve(
                "chat-123",
                ApprovalDecision(step_name="s1", decision="approved"),
            )


class TestGetAuthzContext:
    """Tests for get_authz_context()."""

    @pytest.mark.asyncio
    async def test_get_authz_context_delegates(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """get_authz_context() returns context from store."""
        mock_run_store.get.return_value = {
            "authz_context": {"user_id": "u1", "workflow_name": "chat"},
        }

        ctx = await runner.get_authz_context("chat-123")
        assert ctx == {"user_id": "u1", "workflow_name": "chat"}


class TestGetWorkflowContext:
    """Tests for get_workflow_context()."""

    @pytest.mark.asyncio
    async def test_get_workflow_context_delegates(
        self, runner: ChatWorkflowRunner, mock_run_store: AsyncMock
    ) -> None:
        """get_workflow_context() returns context from store."""
        mock_run_store.get.return_value = {
            "workflow_context": {"config": "value"},
        }

        ctx = await runner.get_workflow_context("chat-123")
        assert ctx == {"config": "value"}


class TestGetStepTranscripts:
    """Tests for get_step_transcripts()."""

    @pytest.mark.asyncio
    async def test_get_step_transcripts_delegates(
        self,
        runner: ChatWorkflowRunner,
        mock_transcript_store: AsyncMock,
    ) -> None:
        """get_step_transcripts() lists and retrieves from transcript store."""
        from cloud_agents.workflow.core.models import StepTranscript

        mock_transcript_store.list_steps.return_value = ["turn-0"]
        mock_transcript_store.get.return_value = StepTranscript(
            step_name="turn-0",
            events=[],
            input_tokens=10,
            output_tokens=5,
        )

        transcripts = await runner.get_step_transcripts("chat-123")
        assert "turn-0" in transcripts
