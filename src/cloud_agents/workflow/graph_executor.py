"""Graph-based workflow executor using pydantic-graph.

Alternative executor for exploratory evaluation. Same-process only —
cannot survive pod restarts. WorkflowExecutor remains the production
executor with durable persistence.
"""

from __future__ import annotations

import logging
import uuid
import warnings
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from agents.registry import AgentRegistry
from agents.remote_agent_client import RemoteAgentClient
from agents.spawner.base import AgentSpawner
from agents.workflow.advisory import AdvisoryEnforcer
from agents.workflow.auto_approve import ApprovalPolicy
from agents.workflow.definition import WorkflowDefinition
from agents.workflow.graph_builder_factory import build_workflow_graph
from agents.workflow.graph_state import GraphWorkflowDeps, GraphWorkflowState
from agents.workflow.graph_steps import APPROVAL_NEEDED_SENTINEL
from agents.workflow.persistence import InMemoryPersistence, WorkflowPersistence
from agents.workflow.state import WorkflowState

warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydantic_graph")

logger = logging.getLogger(__name__)


class GraphExecutor:
    """Workflow executor using pydantic-graph GraphBuilder.

    Same-process exploratory executor. Implements the same runtime
    interface as WorkflowExecutor but uses pydantic-graph for
    step execution topology.

    Limitation: approval pause/resume holds the graph run in memory.
    Cannot survive process restarts.
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
        event_callback: Optional[Callable] = None,
    ) -> None:
        """Initialize the graph executor.

        Args:
            definition: Workflow definition from YAML.
            registry: Agent endpoint registry.
            client_factory: Optional factory for creating RemoteAgentClient.
            persistence: Optional state persistence backend.
            approval_policy: Optional approval policy.
            spawner: Optional agent spawner.
            agent_image: Container image for spawned agents.
            advisory: Optional advisory mode enforcer.
            event_callback: Optional event callback for SSE streaming.
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
        self._graph = build_workflow_graph(definition)
        self._states: dict[str, WorkflowState] = {}
        self._paused_at: dict[str, GraphWorkflowState] = {}

    async def run(self, input_prompt: Optional[str] = None) -> WorkflowState:
        """Execute the workflow using pydantic-graph.

        Args:
            input_prompt: Optional initial prompt override.

        Returns:
            Final WorkflowState.
        """
        now = datetime.now(timezone.utc).isoformat()
        workflow_id = str(uuid.uuid4())
        ws = WorkflowState(
            workflow_id=workflow_id,
            workflow_name=self._definition.metadata["name"],
            created_at=now,
            updated_at=now,
        )
        self._states[workflow_id] = ws

        graph_state = GraphWorkflowState(workflow_state=ws)
        deps = self._build_deps()

        result = await self._graph.run(
            state=graph_state,
            deps=deps,
            inputs=input_prompt or "",
        )

        ws.updated_at = datetime.now(timezone.utc).isoformat()
        if ws.status != "paused":
            has_failures = any(
                s.status == "failed" for s in ws.steps.values()
            )
            ws.status = "failed" if has_failures else "completed"
            ws.current_step = None

        if ws.status == "paused":
            self._paused_at[workflow_id] = graph_state
            for step_spec in self._definition.spec.steps:
                sr = ws.steps.get(step_spec.output_key)
                if sr and sr.status == "awaiting_approval":
                    ws.current_step = step_spec.name
                    break

        await self._persistence.save(ws)
        return ws

    async def resume(self, workflow_id: str, approved: bool = True) -> WorkflowState:
        """Resume a paused workflow after human approval.

        Args:
            workflow_id: The workflow to resume.
            approved: Whether the human approved.

        Returns:
            Updated WorkflowState.
        """
        ws = self._states.get(workflow_id)
        if ws is None:
            raise ValueError(f"Workflow {workflow_id} not found")
        if ws.status != "paused":
            raise ValueError(f"Workflow {workflow_id} is not paused")

        now = datetime.now(timezone.utc).isoformat()

        paused_step = ws.current_step
        if paused_step:
            step_spec = next(
                (s for s in self._definition.spec.steps if s.name == paused_step),
                None,
            )
            step_result = ws.steps.get(step_spec.output_key) if step_spec else None

            if not approved:
                if step_result:
                    step_result.status = "failed"
                    step_result.output = {"approved": False}
                    step_result.error = "Approval rejected by human"
                    step_result.completed_at = now
                ws.status = "failed"
                ws.updated_at = now
                await self._persistence.save(ws)
                return ws

            if step_result:
                step_result.status = "completed"
                step_result.output = {"approved": True}
                step_result.completed_at = now

        ws.status = "running"
        ws.updated_at = now

        remaining_steps = self._definition.spec.steps
        if paused_step:
            paused_idx = next(
                (i for i, s in enumerate(remaining_steps) if s.name == paused_step),
                -1,
            )
            remaining_steps = remaining_steps[paused_idx + 1:]

        from agents.workflow.graph_steps import make_agent_step_fn, make_approval_step_fn
        from pydantic_graph import StepContext

        deps = self._build_deps()
        graph_state = GraphWorkflowState(workflow_state=ws)

        for step_spec in remaining_steps:
            if step_spec.type == "agent":
                fn = make_agent_step_fn(step_spec)
            else:
                fn = make_approval_step_fn(step_spec)

            mock_ctx = StepContext(
                state=graph_state, deps=deps, inputs="",
            )
            result = await fn(mock_ctx)

            if result.get("status") == APPROVAL_NEEDED_SENTINEL:
                ws.current_step = step_spec.name
                ws.status = "paused"
                await self._persistence.save(ws)
                return ws

            if ws.status == "failed":
                await self._persistence.save(ws)
                return ws

        has_failures = any(s.status == "failed" for s in ws.steps.values())
        ws.status = "failed" if has_failures else "completed"
        ws.current_step = None
        ws.updated_at = datetime.now(timezone.utc).isoformat()
        await self._persistence.save(ws)
        return ws

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        """Get current workflow state."""
        return self._states.get(workflow_id)

    async def list_workflows(self) -> list[WorkflowState]:
        """List all tracked workflows."""
        return list(self._states.values())

    def _build_deps(self) -> GraphWorkflowDeps:
        """Build the dependency object for graph step functions."""
        return GraphWorkflowDeps(
            registry=self._registry,
            client_factory=self._client_factory,
            spawner=self._spawner,
            agent_image=self._agent_image,
            advisory=self._advisory,
            approval_policy=self._approval_policy,
            persistence=self._persistence,
            event_callback=self._event_callback,
        )
