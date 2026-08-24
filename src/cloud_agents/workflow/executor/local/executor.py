"""In-process workflow runner using pydantic-graph.

Implements WorkflowRunner without Temporal. Uses:
- graph_translator to build executable graphs from YAML
- RunStateStore for persistence (approval pause/resume, status queries)
- step_runner for sandbox execution

No temporalio imports.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from pydantic_graph import EndMarker

from cloud_agents.workflow.executor.base import (
    ApprovalDecision,
    WorkflowRunner,
    WorkflowStatus,
)
from cloud_agents.workflow.executor.graph_translator import (
    WorkflowGraphState,
    build_graph,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class LocalWorkflowRunner(WorkflowRunner):
    """In-process workflow runner backed by pydantic-graph + PostgreSQL.

    Attributes:
        _spawner: Spawner for sandbox lifecycle.
        _store: RunStateStore for persistence.
        _transcript_store: Optional transcript persistence.
        _running: Registry of in-flight workflow tasks.
    """

    def __init__(
        self,
        spawner: Any = None,
        run_state_store: Any = None,
        transcript_store: Any = None,
    ) -> None:
        """Initialize the local workflow runner.

        Parameters:
            spawner: AgentSpawner instance.
            run_state_store: PostgreSQL RunStateStore.
            transcript_store: Optional TranscriptStore.
        """
        self._spawner = spawner
        self._store = run_state_store
        self._transcript_store = transcript_store
        self._running: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self, input: dict[str, Any]) -> str:
        """Start a new workflow execution.

        Translates the YAML definition to a pydantic-graph, persists
        initial state, and launches async execution.
        """
        workflow_id = input.get("workflow_id") or f"wf-{uuid.uuid4().hex[:12]}"
        definition = input["definition"]
        provider = input.get("provider", {})
        sandbox_image = input.get("sandbox_image", "sandbox:latest")
        metadata = definition.get("metadata", {})
        workflow_name = metadata.get("name", "unnamed")

        self._locks[workflow_id] = asyncio.Lock()

        if self._store:
            await self._store.create(
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                definition=definition,
                provider=provider,
                authz_context=input.get("authz_context", {}),
                user_id=input.get("user_id"),
                session_id=input.get("session_id"),
                parent_workflow_id=input.get("parent_workflow_id"),
            )
            await self._store.update_workflow_context(
                workflow_id,
                {
                    "sandbox_image": sandbox_image,
                    "skills_image": input.get("skills_image"),
                    "skills_paths": input.get("skills_paths"),
                    "mcp_servers": input.get("mcp_servers"),
                    "approval_policy": input.get("approval_policy"),
                },
            )

        graph, state = build_graph(
            definition,
            workflow_id=workflow_id,
            provider=provider,
            sandbox_image=sandbox_image,
            skills_image=input.get("skills_image"),
            skills_paths=input.get("skills_paths"),
            mcp_servers=input.get("mcp_servers"),
            spawner=self._spawner,
            transcript_store=self._transcript_store,
            approval_policy=input.get("approval_policy"),
            user_id=input.get("user_id"),
            session_id=input.get("session_id"),
        )

        task = asyncio.create_task(
            self._execute(workflow_id, graph, state),
            name=f"workflow-{workflow_id}",
        )
        self._running[workflow_id] = task

        return workflow_id

    async def _execute(
        self,
        workflow_id: str,
        graph: Any,
        state: WorkflowGraphState,
    ) -> None:
        """Execute the workflow graph, persisting state at each step."""
        persisted_keys: set[str] = set(state.step_results.keys())
        try:
            async with graph.iter(state=state) as run:
                while True:
                    result = await run.next()

                    if isinstance(result, EndMarker):
                        break

                    for task in result:
                        node_id = task.node_id
                        if node_id.startswith("__"):
                            continue
                        if self._store:
                            await self._store.append_event(
                                workflow_id,
                                {"type": "step.started", "step": node_id},
                            )

                    # Persist only new/changed step results
                    if self._store:
                        new_keys = set(state.step_results.keys()) - persisted_keys
                        for key in new_keys:
                            step_result = state.step_results[key]
                            await self._store.update_step(
                                workflow_id,
                                key,
                                status=step_result.get("status", "completed"),
                                output=step_result.get("output"),
                                error=step_result.get("error"),
                            )
                        persisted_keys.update(new_keys)

                    if state.paused_at_step:
                        if self._store:
                            await self._store.set_paused(workflow_id, state.paused_at_step)
                            await self._store.append_event(
                                workflow_id,
                                {
                                    "type": "workflow.paused",
                                    "step": state.paused_at_step,
                                },
                            )
                        return

            if self._store:
                await self._store.append_event(workflow_id, {"type": "workflow.completed"})
                await self._store.mark_terminal(workflow_id, "completed")

        except asyncio.CancelledError:
            if self._store:
                await self._store.mark_terminal(workflow_id, "cancelled")
            raise
        except Exception as exc:
            logger.exception("Workflow '%s' failed", workflow_id)
            if self._store:
                await self._store.append_event(
                    workflow_id,
                    {"type": "workflow.failed", "error": str(exc)},
                )
                await self._store.mark_terminal(workflow_id, "failed")
        finally:
            self._running.pop(workflow_id, None)

    async def approve(
        self,
        workflow_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Send an approval signal to a paused workflow."""
        if not self._store:
            raise RuntimeError("RunStateStore required for approval")

        lock = self._locks.get(workflow_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[workflow_id] = lock

        async with lock:
            return await self._approve_inner(workflow_id, decision)

    async def _approve_inner(
        self,
        workflow_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Inner approval logic — called under per-workflow lock."""
        state = await self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        if state.get("status") != "paused":
            raise RuntimeError(
                f"Workflow '{workflow_id}' is not paused (status={state.get('status')})"
            )
        if state.get("current_step") != decision.step_name:
            raise RuntimeError(
                f"Workflow '{workflow_id}' is paused at '{state.get('current_step')}', "
                f"not '{decision.step_name}'"
            )

        # Look up output_key from the definition (graph keys by output_key, not step name)
        definition = state.get("definition", {})
        step_defs = {s["name"]: s for s in definition.get("spec", {}).get("steps", [])}
        step_def = step_defs.get(decision.step_name, {})
        output_key = step_def.get("output_key", decision.step_name)

        await self._store.update_step(
            workflow_id,
            output_key,
            status="completed",
            output={"approved": decision.decision == "approved"},
        )
        await self._store.append_event(
            workflow_id,
            {
                "type": "step.approved",
                "step": decision.step_name,
                "decision": decision.decision,
            },
        )
        await self._store.resume(workflow_id)

        # Re-launch graph execution from the step after the approval gate
        await self._resume_from_store(workflow_id)

    async def _resume_from_store(self, workflow_id: str) -> None:
        """Rebuild graph and resume execution after approval."""
        state = await self._store.get(workflow_id)
        if state is None:
            return

        definition = state.get("definition", {})
        provider = state.get("provider", {})
        wf_ctx = state.get("workflow_context", {})

        graph, graph_state = build_graph(
            definition,
            workflow_id=workflow_id,
            provider=provider,
            sandbox_image=wf_ctx.get("sandbox_image", "sandbox:latest"),
            skills_image=wf_ctx.get("skills_image"),
            skills_paths=wf_ctx.get("skills_paths"),
            mcp_servers=wf_ctx.get("mcp_servers"),
            spawner=self._spawner,
            transcript_store=self._transcript_store,
            approval_policy=wf_ctx.get("approval_policy"),
        )

        # Clear pause flag so the graph doesn't skip remaining steps
        graph_state.paused_at_step = None

        # Restore prior step results into the graph state
        for key, result in state.get("steps", {}).items():
            graph_state.step_results[key] = result

        # Check if cancelled between resume() and here
        refreshed = await self._store.get(workflow_id)
        if refreshed and refreshed.get("status") == "cancelled":
            return

        task = asyncio.create_task(
            self._execute(workflow_id, graph, graph_state),
            name=f"workflow-{workflow_id}",
        )
        self._running[workflow_id] = task

    async def cancel(self, workflow_id: str) -> None:
        """Cancel a running workflow."""
        if self._store:
            state = await self._store.get(workflow_id)
            if state is None:
                raise KeyError(f"Workflow '{workflow_id}' not found")

        task = self._running.get(workflow_id)
        if task and not task.done():
            task.cancel()
            # Let _execute handle mark_terminal via CancelledError
        elif self._store:
            await self._store.mark_terminal(workflow_id, "cancelled")

    async def get_status(self, workflow_id: str) -> WorkflowStatus:
        """Get the current status of a workflow execution."""
        if not self._store:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        state = await self._store.get(workflow_id)
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
        """Get the authorization context for a workflow."""
        if not self._store:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        state = await self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return state.get("authz_context", {})

    async def get_workflow_context(self, workflow_id: str) -> dict[str, Any]:
        """Get the full workflow context for escalation handoff."""
        if not self._store:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        state = await self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return state.get("workflow_context", {})

    async def get_step_transcripts(self, workflow_id: str) -> dict[str, Any]:
        """Get agent execution transcripts for all completed steps."""
        if self._transcript_store:
            step_names = await self._transcript_store.list_steps(workflow_id)
            transcripts = {}
            for name in step_names:
                t = await self._transcript_store.get(workflow_id, name)
                if t:
                    transcripts[name] = t.model_dump()
            return transcripts

        if not self._store:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        state = await self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return {}

    async def is_terminal(self, workflow_id: str) -> bool:
        """Check whether a workflow has reached a terminal state."""
        if not self._store:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        state = await self._store.get(workflow_id)
        if state is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        return state.get("status", "") in _TERMINAL_STATUSES

    async def recover_paused(self) -> list[str]:
        """Recover workflows paused at approval on startup.

        Returns list of workflow IDs that were found paused.
        """
        if not self._store:
            return []

        paused = await self._store.list_paused()
        if paused:
            logger.info(
                "Found %d paused workflows on startup: %s",
                len(paused),
                paused,
            )
        return paused
