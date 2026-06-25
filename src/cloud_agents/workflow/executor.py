"""Workflow executor — runs multi-step agent workflows.

Iterates through workflow steps sequentially, calling agents via
RemoteAgentClient, handling conditions, and pausing on approval steps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.remote_agent_client import RemoteAgentClient
from agents.workflow.advisory import AdvisoryEnforcer
from agents.workflow.conditions import evaluate_condition
from agents.workflow.definition import WorkflowDefinition, WorkflowStepSpec
from agents.workflow.events import WorkflowEvent
from agents.workflow.interpolation import interpolate
from agents.workflow.persistence import InMemoryPersistence, WorkflowPersistence
from agents.spawner.base import AgentSpawner
from agents.workflow.auto_approve import ApprovalPolicy, classify_step_risk
from agents.workflow.retry import RetryContext, build_escalation
from agents.workflow.state import StepResult, WorkflowState
from agents.runtime.tracing import get_tracer, set_span_error

logger = logging.getLogger(__name__)


class WorkflowExecutor:
    """Executes a multi-step agent workflow.

    Attributes:
        definition: The workflow definition.
        registry: Agent endpoint registry for dispatch.
    """

    def __init__(
        self,
        definition: WorkflowDefinition,
        registry: AgentRegistry,
        client_factory: Optional[Callable[[str], RemoteAgentClient]] = None,
        persistence: Optional[WorkflowPersistence] = None,
        approval_policy: Optional[ApprovalPolicy] = None,
        spawner: Optional[AgentSpawner] = None,
        agent_image: str = "agent-runtime:latest",
        advisory: Optional[AdvisoryEnforcer] = None,
        event_callback: Optional[Callable[[WorkflowEvent], Any]] = None,
    ) -> None:
        """Initialize the executor.

        Args:
            definition: Workflow definition from YAML.
            registry: Agent endpoint registry.
            client_factory: Optional factory for creating RemoteAgentClient.
            persistence: Optional state persistence backend. Defaults to in-memory.
            advisory: Optional advisory mode enforcer.
        """
        self._definition = definition
        self._registry = registry
        self._client_factory = client_factory or (
            lambda agent_name: RemoteAgentClient(registry.get_endpoint(agent_name))
        )
        self._persistence = persistence or InMemoryPersistence()
        self._approval_policy = approval_policy or ApprovalPolicy()
        self._spawner = spawner
        self._agent_image = agent_image
        self._advisory = advisory or AdvisoryEnforcer(
            enabled=definition.metadata.get("mode") == "advisory"
        )
        self._event_callback = event_callback
        self._tracer = get_tracer("agents.workflow.executor")

        self._validate_spawn_config()

    def _validate_spawn_config(self) -> None:
        """Validate that ephemeral agent steps have a spawner configured."""
        for step in self._definition.spec.steps:
            if step.type != "agent":
                continue
            if step.spawn in ("ephemeral", "on-demand") and not self._spawner:
                raise ValueError(
                    f"Step '{step.name}' uses spawn='{step.spawn}' but no spawner "
                    f"is configured. Set spawn: pre-deployed or configure a spawner."
                )

    async def run(self, input_prompt: str | None = None) -> WorkflowState:
        """Execute the workflow from start.

        Args:
            input_prompt: Optional initial prompt override.

        Returns:
            Final WorkflowState (may be paused if an approval step is hit).
        """
        now = datetime.now(timezone.utc).isoformat()
        workflow_id = str(uuid.uuid4())
        state = WorkflowState(
            workflow_id=workflow_id,
            workflow_name=self._definition.metadata["name"],
            definition_snapshot=self._definition.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        await self._persist(state)
        await self._emit(WorkflowEvent(
            event_type="workflow.started", workflow_id=workflow_id,
        ))

        return await self._execute_from(state, start_index=0)

    async def resume(
        self, workflow_id: str, approved: bool = True
    ) -> WorkflowState:
        """Resume a paused workflow after human approval.

        Args:
            workflow_id: The workflow to resume.
            approved: Whether the human approved.

        Returns:
            Updated WorkflowState.
        """
        state = await self._persistence.load(workflow_id)
        if state is None:
            raise ValueError(f"Workflow {workflow_id} not found")
        if state.status != "paused":
            raise ValueError(f"Workflow {workflow_id} is not paused")

        self._check_approval_timeout(state)
        if state.status == "failed":
            return state

        paused_index = state.paused_step_index or 0
        paused_step = self._definition.spec.steps[paused_index]
        step_result = state.steps.get(paused_step.output_key)

        now = datetime.now(timezone.utc).isoformat()
        if not approved:
            if step_result:
                step_result.status = "failed"
                step_result.output = {"approved": False}
                step_result.error = "Approval rejected by human"
                step_result.completed_at = now
            state.status = "failed"
            state.updated_at = now
            await self._persist(state)
            return state

        if step_result:
            step_result.status = "completed"
            step_result.output = {"approved": True}
            step_result.completed_at = now

        state.status = "running"
        state.updated_at = now

        return await self._execute_from(state, start_index=paused_index + 1)

    async def get_state(self, workflow_id: str) -> WorkflowState | None:
        """Get current workflow state. Re-derives status, checks timeouts."""
        state = await self._persistence.load(workflow_id)
        if state:
            derived = WorkflowState.derive_status(state.steps)
            if state.status != derived and state.status not in ("paused",):
                state.status = derived
            self._check_approval_timeout(state)
        return state

    def _check_approval_timeout(self, state: WorkflowState) -> None:
        """Enforce approval timeout on the current step."""
        if state.status != "paused" or not state.current_step:
            return
        paused_index = state.paused_step_index
        if paused_index is None:
            return
        step_spec = self._definition.spec.steps[paused_index]
        step_result = state.steps.get(step_spec.output_key)
        if not step_result or step_result.status != "awaiting_approval":
            return
        if not step_result.started_at:
            return
        started = datetime.fromisoformat(step_result.started_at)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > step_spec.timeout_seconds:
            step_result.status = "failed"
            step_result.error = f"Approval timed out after {step_spec.timeout_seconds}s"
            step_result.completed_at = datetime.now(timezone.utc).isoformat()
            state.status = "failed"
            state.updated_at = datetime.now(timezone.utc).isoformat()

    async def list_workflows(self) -> list[WorkflowState]:
        """List all tracked workflows."""
        return await self._persistence.list_active()

    async def _emit(self, event: WorkflowEvent) -> None:
        """Emit a workflow event via the callback if configured."""
        if self._event_callback:
            try:
                result = self._event_callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.warning("Event callback failed for %s", event.event_type)

    async def _persist(self, state: WorkflowState) -> None:
        """Save workflow state to persistence backend."""
        await self._persistence.save(state)

    async def _execute_from(
        self, state: WorkflowState, start_index: int
    ) -> WorkflowState:
        """Execute steps starting from the given index.

        Args:
            state: Current workflow state.
            start_index: Index in the steps list to start from.

        Returns:
            Updated WorkflowState.
        """
        steps = self._definition.spec.steps

        for i in range(start_index, len(steps)):
            step = steps[i]
            state.current_step = step.name
            state.updated_at = datetime.now(timezone.utc).isoformat()

            if step.condition:
                try:
                    if not evaluate_condition(step.condition, state):
                        state.steps[step.output_key] = StepResult(
                            step_name=step.name, status="skipped"
                        )
                        logger.info("Step '%s' skipped (condition false)", step.name)
                        continue
                except ValueError as exc:
                    logger.error("Condition error on step '%s': %s", step.name, exc)
                    state.steps[step.output_key] = StepResult(
                        step_name=step.name, status="failed",
                        error=f"Condition error: {exc}",
                    )
                    state.status = "failed"
                    return state

            if step.type == "human-approval":
                if self._advisory.should_skip_approval():
                    state.steps[step.output_key] = StepResult(
                        step_name=step.name,
                        status="skipped",
                        output={"advisory": True, "skipped_reason": "advisory mode"},
                    )
                    logger.info("Step '%s' skipped (advisory mode)", step.name)
                    continue

                classification = classify_step_risk(step, self._approval_policy)
                if classification.auto_approved:
                    state.steps[step.output_key] = StepResult(
                        step_name=step.name,
                        status="completed",
                        output={"approved": True, "auto_approved": True, "risk_level": classification.risk_level},
                        started_at=datetime.now(timezone.utc).isoformat(),
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    await self._persist(state)
                    logger.info("Step '%s' auto-approved (risk: %s)", step.name, classification.risk_level)
                    continue

                state.steps[step.output_key] = StepResult(
                    step_name=step.name,
                    status="awaiting_approval",
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
                state.status = "paused"
                state.paused_step_index = i
                await self._persist(state)
                await self._emit(WorkflowEvent(
                    event_type="workflow.paused", workflow_id=state.workflow_id,
                    step_name=step.name,
                ))
                logger.info("Workflow paused at step '%s' for approval (risk: %s)", step.name, classification.risk_level)
                return state

            if step.type == "agent":
                await self._emit(WorkflowEvent(
                    event_type="step.started", workflow_id=state.workflow_id,
                    step_name=step.name,
                ))
                result = await self._execute_agent_step_with_retry(step, state)
                state.steps[step.output_key] = result
                await self._persist(state)
                event_type = "step.completed" if result.status == "completed" else "step.failed"
                await self._emit(WorkflowEvent(
                    event_type=event_type, workflow_id=state.workflow_id,
                    step_name=step.name,
                ))
                if result.status == "failed":
                    state.status = "failed"
                    await self._persist(state)
                    await self._emit(WorkflowEvent(
                        event_type="workflow.failed", workflow_id=state.workflow_id,
                    ))
                    return state

        state.status = "completed"
        state.current_step = None
        state.updated_at = datetime.now(timezone.utc).isoformat()
        await self._persist(state)
        await self._emit(WorkflowEvent(
            event_type="workflow.completed", workflow_id=state.workflow_id,
        ))
        return state

    async def _execute_agent_step_with_retry(
        self, step: WorkflowStepSpec, state: WorkflowState
    ) -> StepResult:
        """Execute an agent step with retry and escalation.

        Retries up to step.max_retries times, passing failure context
        to each subsequent attempt. On exhaustion, generates an
        escalation handoff document.
        """
        retry_ctx = RetryContext(max_attempts=step.max_retries)

        while not retry_ctx.exhausted:
            result = await self._execute_agent_step(step, state, retry_ctx)
            if result.status == "completed":
                return result
            retry_ctx.add_failure(
                result.error or "Unknown error",
                result.output,
            )
            logger.warning(
                "Step '%s' failed (attempt %d/%d): %s",
                step.name, retry_ctx.attempt - 1, retry_ctx.max_attempts,
                result.error,
            )

        escalation = build_escalation(
            workflow_name=self._definition.metadata["name"],
            step_name=step.name,
            retry_ctx=retry_ctx,
            collected_evidence={
                k: v.output for k, v in state.steps.items() if v.output
            },
        )
        logger.error(
            "Step '%s' exhausted %d retries — escalating",
            step.name, retry_ctx.max_attempts,
        )
        return StepResult(
            step_name=step.name,
            status="failed",
            error=f"Retries exhausted ({retry_ctx.max_attempts} attempts). Escalation generated.",
            output=escalation.model_dump(),
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    async def _execute_agent_step(
        self, step: WorkflowStepSpec, state: WorkflowState,
        retry_ctx: RetryContext | None = None,
    ) -> StepResult:
        """Execute a single agent step.

        Args:
            step: The step specification.
            state: Current workflow state (for template interpolation).
            retry_ctx: Optional retry context for enriching the prompt.

        Returns:
            StepResult with the agent's output.
        """
        started_at = datetime.now(timezone.utc).isoformat()

        prompt = step.prompt or ""
        try:
            prompt = interpolate(prompt, state)
        except ValueError as exc:
            return StepResult(
                step_name=step.name, status="failed",
                error=f"Template error: {exc}",
                started_at=started_at,
            )

        if retry_ctx and retry_ctx.attempt > 1:
            prompt = retry_ctx.build_retry_prompt(prompt)

        prompt = self._advisory.annotate_prompt(prompt)

        logger.info("Executing step '%s' with agent '%s' (spawn=%s)", step.name, step.agent, step.spawn)

        spawned_name = None
        with self._tracer.start_as_current_span(f"workflow.step.{step.name}") as span:
            span.set_attribute("step.name", step.name)
            span.set_attribute("step.agent", step.agent or "")
            span.set_attribute("step.spawn", step.spawn)
            try:
                if step.spawn in ("on-demand", "ephemeral") and self._spawner:
                    import hashlib
                    hash_input = f"{state.workflow_id}:{step.name}:1"
                    spawn_id = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
                    spawned_name = f"{step.agent}-{spawn_id}"
                    endpoint = await self._spawner.spawn(
                        spawned_name, self._agent_image,
                        env={
                            "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
                            "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://localhost:11434/v1"),
                        },
                        config=step.spawn_config,
                    )
                    await self._spawner.wait_ready(endpoint)
                    from agents.runtime.auth import get_api_token
                    client = RemoteAgentClient(endpoint, auth_token=get_api_token() or None)
                else:
                    client = self._client_factory(step.agent)
                context: dict[str, Any] = {}
                if self._advisory.enabled:
                    context["advisory_mode"] = True
                if step.permissions:
                    context["allowed_tools"] = step.permissions.allowed_tools
                    context["denied_tools"] = step.permissions.denied_tools
                response = await client.run(prompt, context=context or None)
            except Exception as exc:
                set_span_error(span, exc)
                return StepResult(
                    step_name=step.name, status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            finally:
                if spawned_name and self._spawner:
                    await self._spawner.destroy(spawned_name)

            span.set_attribute("step.status", "completed")
            output = self._advisory.annotate_output(response.output)
            return StepResult(
                step_name=step.name,
                status="completed",
                output=output,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
