"""Step function factories for pydantic-graph workflow execution.

Creates async step functions that run inside pydantic-graph nodes,
handling agent dispatch, approval, spawn, retry, and advisory mode.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic_graph import StepContext

from agents.models import AgentRunResponse
from agents.remote_agent_client import RemoteAgentClient
from agents.workflow.auto_approve import classify_step_risk
from agents.workflow.conditions import evaluate_condition
from agents.workflow.definition import WorkflowStepSpec
from agents.workflow.events import WorkflowEvent
from agents.workflow.graph_state import GraphWorkflowDeps, GraphWorkflowState
from agents.workflow.interpolation import interpolate
from agents.workflow.retry import RetryContext, build_escalation
from agents.workflow.state import StepResult

logger = logging.getLogger(__name__)

APPROVAL_NEEDED_SENTINEL = "__APPROVAL_NEEDED__"


async def _emit_event(deps: GraphWorkflowDeps, event: WorkflowEvent) -> None:
    """Emit a workflow event via deps callback if configured."""
    if deps.event_callback:
        import asyncio
        try:
            result = deps.event_callback(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning("Event callback failed for %s", event.event_type)


def make_agent_step_fn(step_spec: WorkflowStepSpec) -> Any:
    """Create an agent step function for pydantic-graph.

    Args:
        step_spec: The workflow step specification.

    Returns:
        Async function compatible with GraphBuilder.step().
    """

    async def agent_step(
        ctx: StepContext[GraphWorkflowState, GraphWorkflowDeps, Any],
    ) -> dict:
        """Execute an agent step with spawn/retry/advisory support."""
        state = ctx.state.workflow_state
        deps = ctx.deps
        started_at = datetime.now(timezone.utc).isoformat()

        await _emit_event(deps, WorkflowEvent(
            event_type="step.started", workflow_id=state.workflow_id,
            step_name=step_spec.name,
        ))

        if step_spec.condition:
            try:
                if not evaluate_condition(step_spec.condition, state):
                    state.steps[step_spec.output_key] = StepResult(
                        step_name=step_spec.name, status="skipped",
                    )
                    await _emit_event(deps, WorkflowEvent(
                        event_type="step.skipped", workflow_id=state.workflow_id,
                        step_name=step_spec.name,
                    ))
                    return {"status": "skipped", "step": step_spec.name}
            except ValueError as exc:
                state.steps[step_spec.output_key] = StepResult(
                    step_name=step_spec.name, status="failed",
                    error=f"Condition error: {exc}",
                )
                state.status = "failed"
                return {"status": "failed", "step": step_spec.name}

        prompt = step_spec.prompt or ""
        try:
            prompt = interpolate(prompt, state)
        except ValueError as exc:
            state.steps[step_spec.output_key] = StepResult(
                step_name=step_spec.name, status="failed",
                error=f"Template error: {exc}", started_at=started_at,
            )
            state.status = "failed"
            return {"status": "failed", "step": step_spec.name}

        prompt = deps.advisory.annotate_prompt(prompt)

        retry_ctx = RetryContext(max_attempts=step_spec.max_retries)
        while not retry_ctx.exhausted:
            result = await _execute_once(step_spec, prompt, deps, retry_ctx, started_at)
            if result.status == "completed":
                state.steps[step_spec.output_key] = result
                await _emit_event(deps, WorkflowEvent(
                    event_type="step.completed", workflow_id=state.workflow_id,
                    step_name=step_spec.name,
                ))
                return {"status": "completed", "step": step_spec.name}
            retry_ctx.add_failure(result.error or "Unknown error", result.output)
            if retry_ctx.attempt > 1:
                prompt = retry_ctx.build_retry_prompt(step_spec.prompt or "")

        escalation = build_escalation(
            workflow_name=state.workflow_name,
            step_name=step_spec.name,
            retry_ctx=retry_ctx,
            collected_evidence={k: v.output for k, v in state.steps.items() if v.output},
        )
        state.steps[step_spec.output_key] = StepResult(
            step_name=step_spec.name, status="failed",
            error=f"Retries exhausted ({retry_ctx.max_attempts} attempts)",
            output=escalation.model_dump(),
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        state.status = "failed"
        await _emit_event(deps, WorkflowEvent(
            event_type="step.failed", workflow_id=state.workflow_id,
            step_name=step_spec.name,
        ))
        return {"status": "failed", "step": step_spec.name}

    agent_step.__name__ = step_spec.name
    agent_step.__qualname__ = step_spec.name
    return agent_step


async def _execute_once(
    step_spec: WorkflowStepSpec,
    prompt: str,
    deps: GraphWorkflowDeps,
    retry_ctx: RetryContext,
    started_at: str,
) -> StepResult:
    """Execute a single agent call with optional spawning."""
    spawned_name = None
    try:
        spawn_mode = step_spec.spawn
        if spawn_mode in ("on-demand", "ephemeral") and deps.spawner:
            spawn_id = uuid.uuid4().hex[:8]
            spawned_name = f"{step_spec.agent}-{spawn_id}"
            endpoint = await deps.spawner.spawn(
                spawned_name, deps.agent_image,
                env={
                    "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
                    "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://localhost:11434/v1"),
                    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
                },
                config=step_spec.spawn_config,
            )
            await deps.spawner.wait_ready(endpoint)
            client = RemoteAgentClient(endpoint)
        else:
            client = deps.client_factory(step_spec.agent)

        context: dict[str, Any] = {}
        if deps.advisory.enabled:
            context["advisory_mode"] = True
        response = await client.run(prompt, context=context or None)
    except Exception as exc:
        return StepResult(
            step_name=step_spec.name, status="failed",
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        if spawned_name and deps.spawner:
            await deps.spawner.destroy(spawned_name)

    output = deps.advisory.annotate_output(response.output)
    return StepResult(
        step_name=step_spec.name, status="completed",
        output=output,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


def make_approval_step_fn(step_spec: WorkflowStepSpec) -> Any:
    """Create an approval step function for pydantic-graph.

    Args:
        step_spec: The workflow step specification.

    Returns:
        Async function compatible with GraphBuilder.step().
    """

    async def approval_step(
        ctx: StepContext[GraphWorkflowState, GraphWorkflowDeps, Any],
    ) -> dict:
        """Handle human-approval step with auto-approve and advisory skip."""
        state = ctx.state.workflow_state
        deps = ctx.deps

        if deps.advisory.should_skip_approval():
            state.steps[step_spec.output_key] = StepResult(
                step_name=step_spec.name, status="skipped",
                output={"advisory": True, "skipped_reason": "advisory mode"},
            )
            return {"status": "skipped", "step": step_spec.name}

        classification = classify_step_risk(step_spec, deps.approval_policy)
        if classification.auto_approved:
            state.steps[step_spec.output_key] = StepResult(
                step_name=step_spec.name, status="completed",
                output={"approved": True, "auto_approved": True, "risk_level": classification.risk_level},
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"status": "auto_approved", "step": step_spec.name}

        state.steps[step_spec.output_key] = StepResult(
            step_name=step_spec.name, status="awaiting_approval",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        state.status = "paused"
        return {"status": APPROVAL_NEEDED_SENTINEL, "step": step_spec.name}

    approval_step.__name__ = step_spec.name
    approval_step.__qualname__ = step_spec.name
    return approval_step
