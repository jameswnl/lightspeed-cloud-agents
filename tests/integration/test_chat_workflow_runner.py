"""Integration tests for ChatWorkflowRunner.

Tests multi-turn conversation flow end-to-end with mocked LLM
but real runner logic. Verifies context accumulation, transcript
persistence, and history retrieval across multiple turns.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture

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


class InMemoryRunStateStore:
    """In-memory run state store for integration tests."""

    def __init__(self) -> None:
        """Initialize with empty store."""
        self._store: dict[str, dict[str, Any]] = {}

    async def create(
        self,
        workflow_id: str,
        workflow_name: str,
        definition: dict[str, Any],
        provider: dict[str, Any],
        authz_context: dict[str, Any],
        user_id: str | None = None,
        session_id: str | None = None,
        parent_workflow_id: str | None = None,
    ) -> None:
        """Create a new workflow run state."""
        if workflow_id in self._store:
            raise ValueError(f"Workflow '{workflow_id}' already exists")
        self._store[workflow_id] = {
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "status": "running",
            "current_step": None,
            "steps": {},
            "events": [],
            "definition": definition,
            "provider": provider,
            "authz_context": authz_context,
            "workflow_context": {},
            "user_id": user_id,
            "session_id": session_id,
        }

    async def get(self, workflow_id: str) -> dict[str, Any] | None:
        """Retrieve workflow state."""
        return self._store.get(workflow_id)

    async def update_step(
        self,
        workflow_id: str,
        step_name: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Update a step result."""
        state = self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        state["steps"][step_name] = {
            "status": status,
            "output": output,
            "error": error,
        }
        state["current_step"] = step_name

    async def mark_terminal(self, workflow_id: str, status: str) -> None:
        """Mark workflow as terminal."""
        state = self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        state["status"] = status

    async def append_event(self, workflow_id: str, event: dict[str, Any]) -> None:
        """Append an event."""
        state = self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        state["events"].append(event)


class InMemoryTranscriptStore:
    """In-memory transcript store for integration tests."""

    def __init__(self) -> None:
        """Initialize with empty store."""
        self._store: dict[str, list[dict[str, Any]]] = {}

    async def save(
        self,
        workflow_id: str,
        step_name: str,
        transcript: Any,
        trace_id: str | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Save a step transcript with messages."""
        if workflow_id not in self._store:
            self._store[workflow_id] = []
        self._store[workflow_id].append(
            {
                "step_name": step_name,
                "messages": messages,
                "transcript": transcript,
            }
        )

    async def load_recent_turns(self, workflow_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Load recent turns ordered by insertion."""
        entries = self._store.get(workflow_id, [])
        # Return in chronological order, limited
        recent = entries[-limit:] if len(entries) > limit else entries
        return [{"step_name": e["step_name"], "messages": e["messages"]} for e in recent]

    async def list_steps(self, workflow_id: str) -> list[str]:
        """List step names."""
        entries = self._store.get(workflow_id, [])
        return [e["step_name"] for e in entries]

    async def get(self, workflow_id: str, step_name: str) -> Any:
        """Get a specific transcript."""
        entries = self._store.get(workflow_id, [])
        for e in entries:
            if e["step_name"] == step_name:
                return e["transcript"]
        return None


@pytest.fixture(name="run_store")
def run_store_fixture() -> InMemoryRunStateStore:
    """In-memory run state store."""
    return InMemoryRunStateStore()


@pytest.fixture(name="transcript_store")
def transcript_store_fixture() -> InMemoryTranscriptStore:
    """In-memory transcript store."""
    return InMemoryTranscriptStore()


@pytest.fixture(name="runner")
def runner_fixture(
    run_store: InMemoryRunStateStore,
    transcript_store: InMemoryTranscriptStore,
) -> ChatWorkflowRunner:
    """ChatWorkflowRunner with in-memory stores."""
    config = ChatWorkflowConfig(
        provider=_PROVIDER,
        system_prompt="You are a helpful assistant.",
        max_context_turns=20,
    )
    return ChatWorkflowRunner(
        run_store=run_store,
        transcript_store=transcript_store,
        config=config,
    )


class TestMultiTurnConversation:
    """End-to-end multi-turn conversation tests."""

    @pytest.mark.asyncio
    async def test_three_turn_conversation(
        self,
        runner: ChatWorkflowRunner,
        transcript_store: InMemoryTranscriptStore,
        mocker: MockerFixture,
    ) -> None:
        """Run a 3-turn conversation and verify all turns are saved."""
        responses = [
            {"response": "Hello! How can I help?"},
            {"response": "Python is a great language!"},
            {"response": "Sure, here's an example: print('hello')"},
        ]
        call_count = 0

        def make_mock_executor() -> Any:
            nonlocal call_count
            mock_exec = mocker.AsyncMock()

            async def run_side_effect(step_input: Any) -> StepResult:
                nonlocal call_count
                result = StepResult(
                    status="completed",
                    output=responses[call_count],
                    input_tokens=10 * (call_count + 1),
                    output_tokens=5 * (call_count + 1),
                    duration_ms=100 * (call_count + 1),
                )
                call_count += 1
                return result

            mock_exec.run.side_effect = run_side_effect
            return mock_exec

        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            side_effect=lambda step_def, spawner: make_mock_executor(),
        )

        # Start conversation
        wf_id = await runner.start({"workflow_id": "conv-1", "user_id": "alice"})
        assert wf_id == "conv-1"

        # Turn 1
        r1 = await runner.send_message("conv-1", "Hi there!")
        assert r1.status == "completed"
        assert r1.output == {"response": "Hello! How can I help?"}

        # Turn 2
        r2 = await runner.send_message("conv-1", "Tell me about Python")
        assert r2.status == "completed"
        assert r2.output == {"response": "Python is a great language!"}

        # Turn 3
        r3 = await runner.send_message("conv-1", "Show me an example")
        assert r3.status == "completed"

        # Verify all 3 turns were saved
        steps = await transcript_store.list_steps("conv-1")
        assert steps == ["turn-0", "turn-1", "turn-2"]

    @pytest.mark.asyncio
    async def test_context_grows_with_each_turn(
        self,
        runner: ChatWorkflowRunner,
        mocker: MockerFixture,
    ) -> None:
        """Verify context passed to executor grows with each turn."""
        captured_inputs: list[Any] = []

        def make_mock_executor() -> Any:
            mock_exec = mocker.AsyncMock()

            async def run_side_effect(step_input: Any) -> StepResult:
                captured_inputs.append(step_input)
                return StepResult(
                    status="completed",
                    output={"response": f"Reply #{len(captured_inputs)}"},
                )

            mock_exec.run.side_effect = run_side_effect
            return mock_exec

        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            side_effect=lambda step_def, spawner: make_mock_executor(),
        )

        await runner.start({"workflow_id": "conv-2", "user_id": "bob"})

        await runner.send_message("conv-2", "First message")
        await runner.send_message("conv-2", "Second message")
        await runner.send_message("conv-2", "Third message")

        # Turn 0 should have no context
        assert len(captured_inputs[0].context) == 0

        # Turn 1 should have 1 prior turn in context
        assert len(captured_inputs[1].context) == 1
        assert "turn-0" in captured_inputs[1].context

        # Turn 2 should have 2 prior turns in context
        assert len(captured_inputs[2].context) == 2
        assert "turn-0" in captured_inputs[2].context
        assert "turn-1" in captured_inputs[2].context

    @pytest.mark.asyncio
    async def test_get_history_returns_all_messages_in_order(
        self,
        runner: ChatWorkflowRunner,
        mocker: MockerFixture,
    ) -> None:
        """get_history() returns all messages from all turns in order."""
        turn_counter = 0

        def make_mock_executor() -> Any:
            nonlocal turn_counter
            mock_exec = mocker.AsyncMock()

            async def run_side_effect(step_input: Any) -> StepResult:
                nonlocal turn_counter
                turn_counter += 1
                return StepResult(
                    status="completed",
                    output={"response": f"Reply {turn_counter}"},
                )

            mock_exec.run.side_effect = run_side_effect
            return mock_exec

        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            side_effect=lambda step_def, spawner: make_mock_executor(),
        )

        await runner.start({"workflow_id": "conv-3", "user_id": "carol"})
        await runner.send_message("conv-3", "Hello")
        await runner.send_message("conv-3", "How are you?")

        history = await runner.get_history("conv-3")

        assert len(history) == 4
        assert history[0].role == "user"
        assert history[0].content == "Hello"
        assert history[1].role == "assistant"
        assert history[2].role == "user"
        assert history[2].content == "How are you?"
        assert history[3].role == "assistant"


class TestFactoryIntegration:
    """Tests for factory chat engine creation."""

    def test_factory_creates_chat_runner(self, mocker: MockerFixture) -> None:
        """create_runner() with WORKFLOW_ENGINE=chat returns ChatWorkflowRunner."""
        import os

        from cloud_agents.workflow.executor.factory import create_runner

        mocker.patch.dict(
            os.environ,
            {
                "WORKFLOW_ENGINE": "chat",
                "CHAT_PROVIDER_NAME": "openai",
                "CHAT_PROVIDER_MODEL": "gpt-4o",
                "CHAT_PROVIDER_SECRET": "test-key",
            },
        )

        mock_store = mocker.MagicMock()
        mock_transcript = mocker.MagicMock()

        runner = create_runner(
            run_state_store=mock_store,
            transcript_store=mock_transcript,
        )

        assert isinstance(runner, ChatWorkflowRunner)

    def test_factory_chat_requires_run_state_store(self, mocker: MockerFixture) -> None:
        """create_runner() with chat engine requires RunStateStore."""
        import os

        from cloud_agents.workflow.executor.factory import create_runner

        mocker.patch.dict(os.environ, {"WORKFLOW_ENGINE": "chat"})

        with pytest.raises(ValueError, match="requires a RunStateStore"):
            create_runner(transcript_store=mocker.MagicMock())

    def test_factory_chat_requires_transcript_store(self, mocker: MockerFixture) -> None:
        """create_runner() with chat engine requires TranscriptStore."""
        import os

        from cloud_agents.workflow.executor.factory import create_runner

        mocker.patch.dict(os.environ, {"WORKFLOW_ENGINE": "chat"})

        with pytest.raises(ValueError, match="requires a TranscriptStore"):
            create_runner(run_state_store=mocker.MagicMock())


class TestStreamingConversation:
    """Integration tests for streaming multi-turn conversations."""

    @pytest.mark.asyncio
    async def test_streaming_two_turn_conversation(
        self,
        runner: ChatWorkflowRunner,
        transcript_store: InMemoryTranscriptStore,
        mocker: MockerFixture,
    ) -> None:
        """Streaming conversation saves transcripts correctly."""
        turn_counter = 0

        def make_mock_executor() -> Any:
            nonlocal turn_counter
            mock_exec = mocker.AsyncMock()

            async def mock_run_stream(step_input: Any) -> Any:
                nonlocal turn_counter
                turn_counter += 1
                yield StreamEvent(type="token", data={"delta": f"token-{turn_counter}"})
                yield StreamEvent(
                    type="complete",
                    result=StepResult(
                        status="completed",
                        output={"response": f"stream-reply-{turn_counter}"},
                        input_tokens=10,
                        output_tokens=5,
                        duration_ms=50,
                    ),
                )

            mock_exec.run_stream = mock_run_stream
            return mock_exec

        mocker.patch(
            "cloud_agents.workflow.executor.chat.runner.get_step_executor",
            side_effect=lambda step_def, spawner: make_mock_executor(),
        )

        await runner.start({"workflow_id": "stream-conv-1", "user_id": "dave"})

        # Turn 1 streaming
        events1 = []
        async for event in runner.send_message_stream("stream-conv-1", "Hi"):
            events1.append(event)
        assert len(events1) == 2  # token + complete

        # Turn 2 streaming
        events2 = []
        async for event in runner.send_message_stream("stream-conv-1", "More"):
            events2.append(event)
        assert len(events2) == 2

        # Verify both turns saved
        steps = await transcript_store.list_steps("stream-conv-1")
        assert steps == ["turn-0", "turn-1"]
