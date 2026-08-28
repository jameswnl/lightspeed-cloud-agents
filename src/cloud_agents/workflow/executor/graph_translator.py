"""Translate workflow YAML definitions to pydantic-graph executables.

Converts WorkflowDefinition steps into a pydantic-graph Graph that
can be executed in-process. Each step type maps to a graph node:

- type: agent -> dispatches to StepExecutor based on spawn mode
- type: human-approval -> signals pause for approval

No temporalio imports. Used by the LocalWorkflowRunner.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.trace import Link
from pydantic_graph import GraphBuilder, StepContext

from cloud_agents.runtime.tracing import extract_traceparent, get_tracer
from cloud_agents.workflow.core.conditions import evaluate_condition
from cloud_agents.workflow.core.interpolation import interpolate
from cloud_agents.workflow.core.state import StepResult, WorkflowState
from cloud_agents.workflow.executor.middleware import (
    MiddlewareExecutor,
    TracingMiddleware,
    TranscriptMiddleware,
)
from cloud_agents.workflow.executor.step.base import StepInput, StepMetadata
from cloud_agents.workflow.executor.step.dispatch import get_step_executor

logger = logging.getLogger(__name__)

_tracer = get_tracer("cloud_agents.workflow.executor.graph_translator")


@dataclass
class WorkflowGraphState:
    """Shared state flowing through the pydantic-graph execution.

    Attributes:
        workflow_id: Unique workflow execution ID.
        workflow_name: Name from workflow definition metadata.
        step_defs: Step definitions keyed by step name.
        step_results: Step outputs keyed by output_key.
        events: Ordered lifecycle events.
        provider: Provider configuration dict.
        sandbox_image: Sandbox container image.
        skills_image: Optional skills OCI image.
        skills_paths: Optional skills paths within the image.
        mcp_servers: Optional MCP server configurations.
        spawner: Optional spawner instance for sandbox lifecycle.
        transcript_store: Optional transcript persistence store.
        approval_policy: Optional approval policy configuration.
        paused_at_step: Set when execution pauses for approval.
        approval_result: Set when approval is received.
        trace_parent: W3C traceparent of the most recently executed step's
            span. Updated after every step; the runner persists it when the
            workflow pauses, for span-link continuation on resume.
        resume_trace_parent: Traceparent captured before a prior pause, read
            back by the runner on resume. The first step to execute reads
            and clears this to link its span back to the pre-pause trace.
    """

    workflow_id: str = ""
    workflow_name: str = ""
    step_defs: dict[str, dict[str, Any]] = field(default_factory=dict)
    step_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    provider: dict[str, Any] = field(default_factory=dict)
    sandbox_image: str = "sandbox:latest"
    skills_image: Optional[str] = None
    skills_paths: Optional[list[str]] = None
    mcp_servers: Optional[list[dict[str, Any]]] = None
    spawner: Any = None
    transcript_store: Any = None
    approval_policy: Optional[dict[str, Any]] = None
    user_id: Optional[str] = None
    trace_parent: Optional[str] = None
    resume_trace_parent: Optional[str] = None
    session_id: Optional[str] = None
    paused_at_step: Optional[str] = None
    approval_result: Optional[dict[str, Any]] = None


def build_graph(
    definition: dict[str, Any],
    *,
    workflow_id: str = "",
    provider: Optional[dict[str, Any]] = None,
    sandbox_image: str = "sandbox:latest",
    skills_image: Optional[str] = None,
    skills_paths: Optional[list[str]] = None,
    mcp_servers: Optional[list[dict[str, Any]]] = None,
    spawner: Any = None,
    transcript_store: Any = None,
    approval_policy: Optional[dict[str, Any]] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> tuple[Any, WorkflowGraphState]:
    """Translate a workflow definition dict into a pydantic-graph Graph.

    Parameters:
        definition: Full workflow definition (apiVersion, kind, metadata, spec).
        workflow_id: Unique execution ID.
        provider: LLM provider configuration.
        sandbox_image: Container image for sandbox pods.
        skills_image: Optional OCI image for skills.
        skills_paths: Optional paths within skills image.
        mcp_servers: Optional MCP server configurations.
        spawner: Optional spawner for sandbox lifecycle.
        transcript_store: Optional transcript persistence store.
        approval_policy: Optional approval policy config.

    Returns:
        Tuple of (Graph, WorkflowGraphState).
    """
    spec = definition.get("spec", {})
    steps = spec.get("steps", [])
    metadata = definition.get("metadata", {})
    workflow_name = metadata.get("name", "unnamed")

    step_defs = {}
    for step in steps:
        step_defs[step["name"]] = step
        if step.get("parallel_group"):
            logger.warning(
                "Step '%s' has parallel_group '%s' -- not yet supported in "
                "local executor, will run sequentially",
                step["name"],
                step["parallel_group"],
            )

    state = WorkflowGraphState(
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        step_defs=step_defs,
        provider=provider or {},
        sandbox_image=sandbox_image,
        skills_image=skills_image,
        skills_paths=skills_paths,
        mcp_servers=mcp_servers,
        spawner=spawner,
        transcript_store=transcript_store,
        approval_policy=approval_policy,
        user_id=user_id,
        session_id=session_id,
    )

    builder = GraphBuilder(
        state_type=WorkflowGraphState,
        output_type=dict,
    )

    step_nodes = []
    for step in steps:
        step_name = step["name"]
        step_type = step.get("type", "agent")

        if step_type == "human-approval":
            node = _build_approval_step(builder, step_name, step)
        else:
            node = _build_agent_step(builder, step_name, step)

        step_nodes.append(node)

    # Wire edges: start -> step1 -> step2 -> ... -> end
    if step_nodes:
        edges = [builder.edge_from(builder.start_node).to(step_nodes[0])]
        for i in range(len(step_nodes) - 1):
            edges.append(builder.edge_from(step_nodes[i]).to(step_nodes[i + 1]))
        edges.append(builder.edge_from(step_nodes[-1]).to(builder.end_node))
        builder.add(*edges)

    graph = builder.build()
    return graph, state


def _to_workflow_state(state: WorkflowGraphState) -> WorkflowState:
    """Convert graph state to WorkflowState for condition evaluation."""
    steps = {}
    for key, result in state.step_results.items():
        steps[key] = StepResult(
            step_name=key,
            status=result.get("status", "completed"),
            output=result.get("output"),
            error=result.get("error"),
        )
    return WorkflowState(
        workflow_id=state.workflow_id,
        workflow_name=state.workflow_name,
        steps=steps,
        created_at="",
        updated_at="",
    )


def _interpolate_step_text(template: str, wf_state: WorkflowState) -> str:
    """Interpolate {{ steps.X.output.path }} placeholders, failing open.

    Mirrors the Temporal executor's ``_interpolate_prompt`` error handling:
    an unresolvable reference (missing step, missing output, bad path)
    falls back to the raw template rather than crashing the step.
    """
    if "{{" not in template:
        return template
    try:
        return interpolate(template, wf_state)
    except ValueError as exc:
        logger.debug("Template interpolation failed for %r: %s", template, exc)
        return template


def _build_agent_step(
    builder: GraphBuilder,
    step_name: str,
    step: dict[str, Any],
) -> Any:
    """Create an agent step node that calls step_runner.run_step."""

    @builder.step(node_id=step_name)
    async def agent_step(ctx: StepContext[WorkflowGraphState, None, Any]) -> dict:
        """Execute an agent step via StepExecutor with middleware."""
        state = ctx.state
        step_def = state.step_defs[step_name]

        output_key = step_def.get("output_key", step_name)

        # Skip if already completed (resume after approval)
        existing = state.step_results.get(output_key, {})
        if existing.get("status") in ("completed", "failed"):
            logger.info(
                "Step '%s' already %s -- skipping on resume",
                step_name,
                existing["status"],
            )
            return existing

        if state.paused_at_step:
            state.step_results[output_key] = {
                "status": "skipped",
                "reason": "workflow_paused",
            }
            return {"status": "skipped"}

        wf_state = _to_workflow_state(state)

        if condition := step_def.get("condition"):
            if not evaluate_condition(condition, wf_state):
                output_key = step_def.get("output_key", step_name)
                state.step_results[output_key] = {"status": "skipped"}
                logger.info("Step '%s' skipped -- condition not met", step_name)
                return {"status": "skipped"}

        executor = get_step_executor(
            step=step_def,
            spawner=state.spawner,
            transcript_store=state.transcript_store,
        )

        raw_instructions = step_def.get("instructions")
        step_input = StepInput(
            prompt=_interpolate_step_text(step_def.get("prompt", ""), wf_state),
            provider=state.provider,
            system_prompt=(
                _interpolate_step_text(raw_instructions, wf_state)
                if raw_instructions
                else raw_instructions
            ),
            output_schema=step_def.get("output_schema"),
            tools=step_def.get("tools", []),
            tools_module=os.environ.get("CLOUD_AGENTS_TOOLS_MODULE"),
            context=state.step_results,
            timeout_seconds=step_def.get("timeout_seconds", 600),
            sandbox_image=state.sandbox_image,
            skills_image=state.skills_image,
            skills_paths=state.skills_paths,
            allowed_skills=step_def.get("allowed_skills"),
            mcp_servers=state.mcp_servers,
            workflow_id=state.workflow_id,
            raw_step=step_def,
            step_name=step_name,
            output_key=output_key,
            metadata=StepMetadata(
                user_id=state.user_id,
                session_id=state.session_id,
            ),
        )

        # Build middleware stack: tracing + optional transcript persistence
        middlewares = [TracingMiddleware()]
        if state.transcript_store:
            middlewares.append(TranscriptMiddleware(state.transcript_store))

        # Link this step's span back to the pre-pause trace, once, on resume
        links = None
        if state.resume_trace_parent:
            span_context = trace.get_current_span(
                extract_traceparent({"traceparent": state.resume_trace_parent})
            ).get_span_context()
            if span_context and span_context.is_valid:
                links = [Link(span_context)]
            state.resume_trace_parent = None

        wrapped = MiddlewareExecutor(executor, middlewares, tracer=_tracer, links=links)
        exec_result = await wrapped.run(step_input)

        # Always overwrite (not just when truthy) so a step whose capture
        # failed doesn't leave a stale, unrelated earlier step's trace_parent
        # in place -- a pause right after must not link to the wrong span.
        state.trace_parent = (
            step_input.metadata.extra.get("trace_parent")
            if step_input.metadata is not None
            else None
        )

        state.step_results[output_key] = {
            "status": exec_result.status,
            "output": exec_result.output,
            "error": exec_result.error,
        }

        return {
            "status": exec_result.status,
            "output": exec_result.output,
            "error": exec_result.error,
        }

    return agent_step


def _build_approval_step(
    builder: GraphBuilder,
    step_name: str,
    step: dict[str, Any],
) -> Any:
    """Create a human-approval step node that pauses execution."""

    @builder.step(node_id=step_name)
    async def approval_step(
        ctx: StepContext[WorkflowGraphState, None, Any],
    ) -> dict:
        """Pause for human approval."""
        state = ctx.state
        step_def = state.step_defs[step_name]
        output_key = step_def.get("output_key", step_name)

        # Skip if already completed (resume after approval)
        existing = state.step_results.get(output_key, {})
        if existing.get("status") == "completed":
            return {"status": "completed", "output": existing.get("output")}

        auto_approve = False
        if state.approval_policy:
            auto_approve = state.approval_policy.get("auto_approve", False)

        if auto_approve:
            result = {"approved": True, "auto_approved": True}
            state.step_results[output_key] = {
                "status": "completed",
                "output": result,
            }
            return {"status": "completed", "output": result}

        # Interpolate the approval message (mirrors the Temporal executor's
        # _handle_approval and the #196 fix for agent-step prompts) and store
        # it in step output -- the only surfacing mechanism the local runner
        # has, since there's no notifier_config/send_approval_notification
        # equivalent here. A status/query endpoint already returns
        # WorkflowStatus.steps[output_key].output, so this reaches a UI or
        # CLI without any new persistence or API surface (#197). Like the
        # rest of `interpolate()`'s callers (LLM prompts, Temporal's approval
        # notifications), substituted values stay wrapped in <data>...</data>
        # -- a UI/CLI rendering this for a human should account for that
        # wrapper rather than displaying it raw. Only visible while this step
        # is "awaiting_approval": _approve_inner() overwrites this output
        # with {"approved": bool} once a decision is recorded, so it's not
        # retained as an audit field after resume. An explicit empty-string
        # `message` is treated the same as an omitted one (no message),
        # consistent with how `instructions` is handled for agent steps.
        raw_message = step_def.get("message")
        message = (
            _interpolate_step_text(raw_message, _to_workflow_state(state))
            if raw_message
            else None
        )

        # Signal pause -- the LocalWorkflowRunner checks paused_at_step
        # after each graph.iter().next() and breaks the loop.
        state.paused_at_step = step_name
        state.step_results[output_key] = {
            "status": "awaiting_approval",
            "output": {"message": message} if message else None,
        }
        return {"status": "paused", "paused_at": step_name}

    return approval_step
