"""ChatWorkflowRunner -- multi-turn chat orchestrator.

Implements the WorkflowRunner ABC for interactive conversations.
Each user message is a "step" executed via StepExecutor. Prior turns
flow as context to subsequent steps. Conversation state is persisted
in RunStateStore (lifecycle) and TranscriptStore (turn content).

No temporalio imports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional

from cloud_agents.workflow.core.models import StepTranscript, TranscriptEvent
from cloud_agents.workflow.executor.base import (
    ApprovalDecision,
    WorkflowRunner,
    WorkflowStatus,
)
from cloud_agents.workflow.executor.middleware import (
    MiddlewareExecutor,
    TracingMiddleware,
)
from cloud_agents.workflow.executor.step.base import (
    StepInput,
    StepMetadata,
    StepResult,
    StreamEvent,
)
from cloud_agents.workflow.executor.step.conversation import ConversationMessage
from cloud_agents.workflow.executor.step.dispatch import get_step_executor

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass
class ChatWorkflowConfig:
    """Configuration for ChatWorkflowRunner.

    Attributes:
        provider: LLM provider config (name, model, credentials_secret).
        system_prompt: Optional system prompt for all turns.
        tools: Tool names available in conversations.
        tools_module: Module path for tool loading in subprocess.
        mcp_servers: Optional MCP server configs.
        max_context_turns: Max prior turns to include as context.
        spawn: Spawn mode for step execution.
    """

    provider: dict[str, Any]
    system_prompt: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    tools_module: Optional[str] = None
    mcp_servers: Optional[list[dict[str, Any]]] = None
    max_context_turns: int = 20
    timeout_seconds: int = 600
    spawn: str = "none"


class ChatWorkflowRunner(WorkflowRunner):
    """Multi-turn chat as a dynamic workflow.

    Each user message becomes a step executed via StepExecutor. Prior
    turns are loaded from TranscriptStore and passed as context to the
    agent. Conversation lifecycle is tracked in RunStateStore.

    Attributes:
        _run_store: RunStateStore for workflow lifecycle persistence.
        _transcript_store: TranscriptStore for turn content persistence.
        _config: Chat-specific configuration.
        _spawner: Optional spawner for sandbox-based execution.
    """

    def __init__(
        self,
        run_store: Any,
        transcript_store: Any,
        config: ChatWorkflowConfig,
        spawner: Any = None,
        middlewares: Optional[list[Any]] = None,
    ) -> None:
        """Initialize the chat workflow runner.

        Parameters:
            run_store: RunStateStore instance for lifecycle persistence.
            transcript_store: TranscriptStore instance for turn content.
            config: Chat-specific configuration.
            spawner: Optional AgentSpawner for sandbox execution.
            middlewares: Optional list of StepMiddleware instances to apply
                before TracingMiddleware on every turn. Custom middleware
                runs outermost (first in before, last in after);
                TracingMiddleware stays innermost so it always executes.
        """
        self._run_store = run_store
        self._transcript_store = transcript_store
        self._config = config
        self._spawner = spawner
        self._extra_middlewares: list[Any] = middlewares or []
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self, input: dict[str, Any]) -> str:
        """Create a new conversation workflow.

        Parameters:
            input: Dict with optional workflow_id, user_id, session_id.

        Returns:
            Conversation workflow ID.
        """
        workflow_id = input.get("workflow_id") or f"chat-{uuid.uuid4().hex[:12]}"
        user_id = input.get("user_id")
        session_id = input.get("session_id")

        await self._run_store.create(
            workflow_id=workflow_id,
            workflow_name="chat",
            definition={},
            provider=self._config.provider,
            authz_context={},
            user_id=user_id,
            session_id=session_id,
        )

        logger.info("Created chat conversation workflow_id=%s", workflow_id)
        return workflow_id

    def _get_lock(self, workflow_id: str) -> asyncio.Lock:
        """Get or create the per-workflow asyncio lock.

        Parameters:
            workflow_id: Conversation workflow ID.

        Returns:
            asyncio.Lock for the given workflow.
        """
        if workflow_id not in self._locks:
            self._locks[workflow_id] = asyncio.Lock()
        return self._locks[workflow_id]

    async def send_message(self, workflow_id: str, prompt: str) -> StepResult:
        """Execute one conversation turn with prior turns as context.

        Acquires a per-workflow lock to serialize concurrent turns,
        loads prior turns from the transcript store, builds context,
        executes the step via StepExecutor, saves the turn transcript,
        and updates the run state.

        Parameters:
            workflow_id: Target conversation/workflow ID.
            prompt: User message text.

        Returns:
            StepResult with the assistant's response and metrics.

        Raises:
            RuntimeError: If the conversation is in a terminal state.
        """
        async with self._get_lock(workflow_id):
            return await self._send_message_unlocked(workflow_id, prompt)

    async def _send_message_unlocked(self, workflow_id: str, prompt: str) -> StepResult:
        """Execute one conversation turn (internal, caller holds lock).

        Parameters:
            workflow_id: Target conversation/workflow ID.
            prompt: User message text.

        Returns:
            StepResult with the assistant's response and metrics.
        """
        await self._check_not_terminal(workflow_id)

        # 1. Load prior turns from transcript store
        prior_turns = await self._transcript_store.load_recent_turns(
            workflow_id, limit=self._config.max_context_turns
        )

        # 2. Build context from prior turns
        context = self._build_context(prior_turns)

        # 3. Determine turn name from highest existing turn
        turn_number = self._next_turn_number(prior_turns)
        turn_name = f"turn-{turn_number}"

        # 4. Load identity from run state store
        state = await self._run_store.get(workflow_id)

        # 5. Build StepInput
        step_input = self._build_step_input(
            prompt=prompt,
            workflow_id=workflow_id,
            turn_name=turn_name,
            context=context,
            user_id=state.get("user_id") if state else None,
            session_id=state.get("session_id") if state else None,
        )

        # 6. Get step executor wrapped with middleware
        # TracingMiddleware only — TranscriptMiddleware is not used here because
        # chat turns have conversation-specific save logic (_save_turn handles
        # ConversationMessage assembly, TranscriptEvent type mapping, and
        # RunStateStore updates that TranscriptMiddleware doesn't cover).
        # Custom middleware runs outermost (before TracingMiddleware) so it
        # executes first in before() and last in after().
        # No tracer is passed, so no span is opened here -- session.id
        # attribute and trace_parent capture (#179) only apply to
        # LocalWorkflowRunner's step spans, not chat turns. Out of scope
        # for #179; revisit if chat turns need span-based tracing.
        step_def = {"spawn": self._config.spawn, "name": turn_name}
        executor = get_step_executor(step_def, self._spawner)
        wrapped = MiddlewareExecutor(
            executor, [*self._extra_middlewares, TracingMiddleware()]
        )

        # 7. Execute
        result = await wrapped.run(step_input)

        # 8. Save turn to transcript store and update run state
        await self._save_turn(workflow_id, turn_name, prompt, result)

        return result

    async def send_message_stream(
        self, workflow_id: str, prompt: str
    ) -> AsyncIterator[StreamEvent]:
        """Execute one conversation turn with streaming.

        Acquires a per-workflow lock, then uses the executor's streaming
        interface. Yields StreamEvent instances as they arrive, then saves
        the turn transcript after the complete/error event.

        Parameters:
            workflow_id: Target conversation/workflow ID.
            prompt: User message text.

        Yields:
            StreamEvent instances (token deltas, then complete or error).

        Raises:
            RuntimeError: If the conversation is in a terminal state.
        """
        async with self._get_lock(workflow_id):
            async for event in self._send_message_stream_unlocked(workflow_id, prompt):
                yield event

    async def _send_message_stream_unlocked(
        self, workflow_id: str, prompt: str
    ) -> AsyncIterator[StreamEvent]:
        """Execute one streaming turn (internal, caller holds lock).

        Parameters:
            workflow_id: Target conversation/workflow ID.
            prompt: User message text.

        Yields:
            StreamEvent instances (token deltas, then complete or error).
        """
        await self._check_not_terminal(workflow_id)

        # 1. Load prior turns from transcript store
        prior_turns = await self._transcript_store.load_recent_turns(
            workflow_id, limit=self._config.max_context_turns
        )

        # 2. Build context from prior turns
        context = self._build_context(prior_turns)

        # 3. Determine turn name from highest existing turn
        turn_number = self._next_turn_number(prior_turns)
        turn_name = f"turn-{turn_number}"

        # 4. Load identity from run state store
        state = await self._run_store.get(workflow_id)

        # 5. Build StepInput
        step_input = self._build_step_input(
            prompt=prompt,
            workflow_id=workflow_id,
            turn_name=turn_name,
            context=context,
            user_id=state.get("user_id") if state else None,
            session_id=state.get("session_id") if state else None,
        )

        # 6. Get step executor wrapped with middleware
        # No tracer passed here either -- see the non-streaming send_message()
        # for why chat turns don't open spans (#179 is LocalWorkflowRunner-only).
        step_def = {"spawn": self._config.spawn, "name": turn_name}
        executor = get_step_executor(step_def, self._spawner)
        wrapped = MiddlewareExecutor(
            executor, [*self._extra_middlewares, TracingMiddleware()]
        )

        # 7. Stream and capture the final result
        final_result: Optional[StepResult] = None

        try:
            async for event in wrapped.run_stream(step_input):
                yield event
                if event.type in ("complete", "error") and event.result:
                    final_result = event.result
        except Exception as exc:
            final_result = StepResult(status="failed", error=str(exc))
            yield StreamEvent(type="error", data={"error": str(exc)}, result=final_result)
        finally:
            # 8. Save turn to transcript store and update run state
            if final_result:
                await self._save_turn(workflow_id, turn_name, prompt, final_result)

    async def get_history(self, workflow_id: str, limit: int = 20) -> list[ConversationMessage]:
        """Load conversation messages from the transcript store.

        Checks run state first. If missing, falls back to checking
        transcripts -- resilient to run state loss after restart.

        Parameters:
            workflow_id: Target conversation/workflow ID.
            limit: Maximum number of turns to load.

        Returns:
            Ordered list of ConversationMessage objects.

        Raises:
            KeyError: If the conversation does not exist in either store.
        """
        state = await self._run_store.get(workflow_id)
        turns = await self._transcript_store.load_recent_turns(workflow_id, limit=limit)

        if state is None and not turns:
            raise KeyError(f"Conversation '{workflow_id}' not found")

        messages: list[ConversationMessage] = []
        for turn in turns:
            for msg_dict in turn.get("messages") or []:
                messages.append(ConversationMessage.from_dict(msg_dict))
        return messages

    async def approve(
        self,
        workflow_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Not supported for chat mode.

        Raises:
            NotImplementedError: Chat workflows do not have approval gates.
        """
        raise NotImplementedError("ChatWorkflowRunner does not support approval gates")

    async def cancel(self, workflow_id: str) -> None:
        """Cancel a conversation workflow.

        Parameters:
            workflow_id: Target conversation/workflow ID.

        Raises:
            KeyError: If the workflow does not exist.
        """
        state = await self._run_store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        await self._run_store.mark_terminal(workflow_id, "cancelled")

    async def get_status(self, workflow_id: str) -> WorkflowStatus:
        """Get the current status of a conversation workflow.

        Parameters:
            workflow_id: Target conversation/workflow ID.

        Returns:
            WorkflowStatus snapshot.

        Raises:
            KeyError: If the workflow does not exist.
        """
        state = await self._run_store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        status = state["status"]
        return WorkflowStatus(
            workflow_id=workflow_id,
            status=status,
            steps=state.get("steps", {}),
            events=state.get("events", []),
            is_terminal=status in _TERMINAL_STATUSES,
        )

    async def get_authz_context(self, workflow_id: str) -> dict[str, Any]:
        """Get the authorization context for a conversation workflow.

        Parameters:
            workflow_id: Target conversation/workflow ID.

        Returns:
            Authorization context dict.

        Raises:
            KeyError: If the workflow does not exist.
        """
        state = await self._run_store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return state.get("authz_context", {})

    async def get_workflow_context(self, workflow_id: str) -> dict[str, Any]:
        """Get the full workflow context for escalation handoff.

        Parameters:
            workflow_id: Target conversation/workflow ID.

        Returns:
            Workflow context dict.

        Raises:
            KeyError: If the workflow does not exist.
        """
        state = await self._run_store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return state.get("workflow_context", {})

    async def get_step_transcripts(self, workflow_id: str) -> dict[str, Any]:
        """Get agent execution transcripts for all conversation turns.

        Parameters:
            workflow_id: Target conversation/workflow ID.

        Returns:
            Transcripts keyed by turn name.

        Raises:
            KeyError: If the workflow does not exist.
        """
        step_names = await self._transcript_store.list_steps(workflow_id)
        transcripts: dict[str, Any] = {}
        for name in step_names:
            t = await self._transcript_store.get(workflow_id, name)
            if t:
                transcripts[name] = t.model_dump()
        return transcripts

    async def is_terminal(self, workflow_id: str) -> bool:
        """Check whether a conversation has reached a terminal state.

        Parameters:
            workflow_id: Target conversation/workflow ID.

        Returns:
            True if the workflow is completed, failed, or cancelled.

        Raises:
            KeyError: If the workflow does not exist.
        """
        state = await self._run_store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return state.get("status", "") in _TERMINAL_STATUSES

    @staticmethod
    def _extract_assistant_text(output: Any) -> str:
        """Extract clean text from step output for assistant message storage.

        Prefers the 'response' key for plain-text output so the conversation
        history contains readable text rather than JSON blobs like
        '{"response": "Hello"}'. Falls back to JSON dump for structured
        output without a 'response' key.

        Parameters:
            output: Step result output (dict, str, or other).

        Returns:
            Clean text content for the assistant message.
        """
        if output is None:
            return ""
        if isinstance(output, dict):
            if "response" in output:
                val = output["response"]
                if val is None:
                    return ""
                if isinstance(val, str):
                    return val
                return json.dumps(val)
            return json.dumps(output)
        if isinstance(output, str):
            return output
        return json.dumps(output)

    async def _check_not_terminal(self, workflow_id: str) -> None:
        """Raise if conversation is in a terminal state."""
        state = await self._run_store.get(workflow_id)
        if state and state.get("status") in _TERMINAL_STATUSES:
            raise RuntimeError(
                f"Conversation '{workflow_id}' is {state['status']} — cannot send messages"
            )

    @staticmethod
    def _next_turn_number(prior_turns: list[dict[str, Any]]) -> int:
        """Derive next turn number from existing turns.

        Uses the highest turn-N number found, not the count of loaded turns.
        This prevents collisions when max_context_turns < total turns.
        """
        max_num = -1
        for turn in prior_turns:
            name = turn.get("step_name", "")
            if name.startswith("turn-"):
                try:
                    num = int(name.split("-", 1)[1])
                    if num > max_num:
                        max_num = num
                except (ValueError, IndexError):
                    pass
        return max_num + 1

    def _build_context(self, prior_turns: list[dict[str, Any]]) -> dict[str, Any]:
        """Build step context from prior conversation turns.

        Parameters:
            prior_turns: List of turn dicts with step_name and messages.

        Returns:
            Context dict keyed by turn name with message output.
        """
        context: dict[str, Any] = {}
        for i, turn in enumerate(prior_turns):
            if turn.get("messages"):
                turn_key = turn.get("step_name", f"turn-{i}")
                context[turn_key] = {
                    "status": "completed",
                    "output": {"messages": turn["messages"]},
                }
        return context

    def _build_step_input(
        self,
        prompt: str,
        workflow_id: str,
        turn_name: str,
        context: dict[str, Any],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> StepInput:
        """Build a StepInput for a conversation turn.

        Parameters:
            prompt: User message text.
            workflow_id: Conversation workflow ID.
            turn_name: Name for this turn (e.g. turn-0).
            context: Prior turn outputs.
            user_id: User identity from run state store.
            session_id: Session identity from run state store.

        Returns:
            StepInput configured for this conversation turn.
        """
        return StepInput(
            prompt=prompt,
            provider=self._config.provider,
            system_prompt=self._config.system_prompt,
            tools=self._config.tools,
            tools_module=self._config.tools_module,
            mcp_servers=self._config.mcp_servers,
            context=context,
            timeout_seconds=self._config.timeout_seconds,
            workflow_id=workflow_id,
            step_name=turn_name,
            output_key=turn_name,
            metadata=StepMetadata(
                user_id=user_id,
                session_id=session_id,
                conversation_id=workflow_id,
            ),
        )

    async def _save_turn(
        self,
        workflow_id: str,
        turn_name: str,
        prompt: str,
        result: StepResult,
    ) -> None:
        """Save a conversation turn to transcript and run state stores.

        Parameters:
            workflow_id: Conversation workflow ID.
            turn_name: Name for this turn.
            prompt: User's message text.
            result: Step execution result.
        """
        # Build conversation messages
        messages: list[dict[str, Any]] = [
            ConversationMessage(role="user", content=prompt).to_dict(),
        ]

        # Extract tool_call/tool_result events from transcript into
        # ConversationMessage entries so they survive across turns.
        for event in (result.transcript or []):
            event_type = event.get("type", "")
            if event_type == "tool_call":
                messages.append(ConversationMessage(
                    role="tool_call",
                    content="",
                    metadata={
                        "tool_name": event.get("tool_name", ""),
                        "args": event.get("args", {}),
                        "tool_call_id": event.get("tool_call_id", ""),
                    },
                ).to_dict())
            elif event_type == "tool_result":
                output = event.get("output", "")
                messages.append(ConversationMessage(
                    role="tool_result",
                    content=json.dumps(output) if isinstance(output, (dict, list)) else str(output),
                    metadata={
                        "tool_name": event.get("tool_name", ""),
                        "tool_call_id": event.get("tool_call_id", ""),
                    },
                ).to_dict())

        if result.output is not None:
            content = self._extract_assistant_text(result.output)
            messages.append(ConversationMessage(role="assistant", content=content).to_dict())

        # Convert result.transcript dicts to TranscriptEvent objects.
        # Map non-standard types (agent.run, agent.stream, llm.call) to "result".
        _VALID_TYPES = frozenset({"tool_call", "tool_result", "thinking", "result", "error"})
        events = [
            TranscriptEvent(
                ts=e.get("ts", ""),
                type=e.get("type") if e.get("type") in _VALID_TYPES else "result",
                data={k: v for k, v in e.items() if k not in ("ts", "type")},
            )
            for e in (result.transcript or [])
        ]

        # Save to transcript store
        transcript = StepTranscript(
            step_name=turn_name,
            events=events,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
        )
        await self._transcript_store.save(
            workflow_id=workflow_id,
            step_name=turn_name,
            transcript=transcript,
            messages=messages,
        )

        # Update run state
        await self._run_store.update_step(
            workflow_id,
            turn_name,
            result.status,
            output=result.output,
            error=result.error,
        )

        logger.debug(
            "Saved turn %s for workflow=%s (status=%s)",
            turn_name,
            workflow_id,
            result.status,
        )
