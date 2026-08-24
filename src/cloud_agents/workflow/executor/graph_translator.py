"""Translate workflow YAML definitions to pydantic-graph executables.

Converts WorkflowDefinition steps into a pydantic-graph Graph that
can be executed in-process. Each step type maps to a graph node:

- type: agent → dispatches to StepExecutor based on spawn mode
- type: human-approval → signals pause for approval

No temporalio imports. Used by the LocalWorkflowRunner.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic_graph import GraphBuilder, StepContext

from cloud_agents.runtime.tracing import get_tracer
from cloud_agents.workflow.core.conditions import evaluate_condition
from cloud_agents.workflow.core.state import StepResult, WorkflowState
from cloud_agents.workflow.executor.step.base import StepInput, StepMetadata
from cloud_agents.workflow.executor.step.conversation import ConversationMessage
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
                "Step '%s' has parallel_group '%s' — not yet supported in local executor, will run sequentially",
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

    # Wire edges: start → step1 → step2 → ... → end
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


def _build_agent_step(
    builder: GraphBuilder,
    step_name: str,
    step: dict[str, Any],
) -> Any:
    """Create an agent step node that calls step_runner.run_step."""

    @builder.step(node_id=step_name)
    async def agent_step(ctx: StepContext[WorkflowGraphState, None, Any]) -> dict:
        """Execute an agent step in a sandbox container."""
        state = ctx.state
        step_def = state.step_defs[step_name]

        output_key = step_def.get("output_key", step_name)

        # Skip if already completed (resume after approval)
        existing = state.step_results.get(output_key, {})
        if existing.get("status") in ("completed", "failed"):
            logger.info("Step '%s' already %s — skipping on resume", step_name, existing["status"])
            return existing

        if state.paused_at_step:
            state.step_results[output_key] = {"status": "skipped", "reason": "workflow_paused"}
            return {"status": "skipped"}

        if condition := step_def.get("condition"):
            wf_state = _to_workflow_state(state)
            if not evaluate_condition(condition, wf_state):
                output_key = step_def.get("output_key", step_name)
                state.step_results[output_key] = {"status": "skipped"}
                logger.info("Step '%s' skipped — condition not met", step_name)
                return {"status": "skipped"}

        executor = get_step_executor(
            step=step_def,
            spawner=state.spawner,
            transcript_store=state.transcript_store,
        )

        step_input = StepInput(
            prompt=step_def.get("prompt", ""),
            provider=state.provider,
            system_prompt=step_def.get("instructions"),
            output_schema=step_def.get("output_schema"),
            tools=step_def.get("tools", []),
            tools_module=os.environ.get("CLOUD_AGENTS_TOOLS_MODULE"),
            context=state.step_results,
            timeout_seconds=step_def.get("timeout_seconds", 600),
            sandbox_image=state.sandbox_image,
            skills_image=state.skills_image,
            skills_paths=state.skills_paths,
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

        with _tracer.start_as_current_span(
            "step.execute",
            attributes={
                "step.name": step_name,
                "step.spawn": step_def.get("spawn", "ephemeral"),
                "workflow.id": state.workflow_id,
                "model": state.provider.get("model", "unknown"),
            },
        ) as span:
            # Propagate trace_id to metadata for downstream correlation
            span_context = span.get_span_context()
            if span_context and span_context.trace_id:
                step_input.metadata.trace_id = format(span_context.trace_id, "032x")

            exec_result = await executor.run(step_input)

            span.set_attribute("step.status", exec_result.status)
            span.set_attribute("step.input_tokens", exec_result.input_tokens)
            span.set_attribute("step.output_tokens", exec_result.output_tokens)

        # Persist conversation messages to transcript store
        if state.transcript_store:
            messages = [
                ConversationMessage(
                    role="user", content=step_input.prompt
                ).to_dict(),
                ConversationMessage(
                    role="assistant",
                    content=json.dumps(exec_result.output) if exec_result.output else "",
                    metadata={
                        "input_tokens": exec_result.input_tokens,
                        "output_tokens": exec_result.output_tokens,
                    },
                ).to_dict(),
            ]

            from cloud_agents.workflow.core.models import StepTranscript

            await state.transcript_store.save(
                workflow_id=state.workflow_id,
                step_name=output_key,
                transcript=StepTranscript(
                    step_name=output_key,
                    input_tokens=exec_result.input_tokens,
                    output_tokens=exec_result.output_tokens,
                    duration_ms=exec_result.duration_ms,
                ),
                trace_id=step_input.metadata.trace_id if step_input.metadata else None,
                messages=messages,
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
    async def approval_step(ctx: StepContext[WorkflowGraphState, None, Any]) -> dict:
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

        # Signal pause — the LocalWorkflowRunner checks paused_at_step
        # after each graph.iter().next() and breaks the loop.
        state.paused_at_step = step_name
        state.step_results[output_key] = {
            "status": "awaiting_approval",
            "output": None,
        }
        return {"status": "paused", "paused_at": step_name}

    return approval_step
