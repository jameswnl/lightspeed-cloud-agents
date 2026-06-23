"""Workflow executor — runs multi-step agent workflows.

Iterates through workflow steps sequentially, calling agents via
RemoteAgentClient, handling conditions, and pausing on approval steps.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.remote_agent_client import RemoteAgentClient
from agents.workflow.conditions import evaluate_condition
from agents.workflow.definition import WorkflowDefinition, WorkflowStepSpec
from agents.workflow.interpolation import interpolate
from agents.workflow.persistence import InMemoryPersistence, WorkflowPersistence
from agents.workflow.state import StepResult, WorkflowState

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
    ) -> None:
        """Initialize the executor.

        Args:
            definition: Workflow definition from YAML.
            registry: Agent endpoint registry.
            client_factory: Optional factory for creating RemoteAgentClient.
            persistence: Optional state persistence backend. Defaults to in-memory.
        """
        self._definition = definition
        self._registry = registry
        self._client_factory = client_factory or (
            lambda agent_name: RemoteAgentClient(registry.get_endpoint(agent_name))
        )
        self._persistence = persistence or InMemoryPersistence()
        self._states: dict[str, WorkflowState] = {}
        self._paused_at: dict[str, int] = {}

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
            created_at=now,
            updated_at=now,
        )
        self._states[workflow_id] = state
        await self._persist(state)

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
        state = self._states.get(workflow_id)
        if state is None:
            raise ValueError(f"Workflow {workflow_id} not found")
        if state.status != "paused":
            raise ValueError(f"Workflow {workflow_id} is not paused")

        paused_index = self._paused_at.get(workflow_id, 0)
        paused_step = self._definition.spec.steps[paused_index]
        step_result = state.steps.get(paused_step.output_key)
        if step_result:
            step_result.status = "completed"
            step_result.output = {"approved": approved}
            step_result.completed_at = datetime.now(timezone.utc).isoformat()

        state.status = "running"
        state.updated_at = datetime.now(timezone.utc).isoformat()

        return await self._execute_from(state, start_index=paused_index + 1)

    async def get_state(self, workflow_id: str) -> WorkflowState | None:
        """Get current workflow state."""
        return self._states.get(workflow_id)

    async def list_workflows(self) -> list[WorkflowState]:
        """List all tracked workflows."""
        return list(self._states.values())

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
                state.steps[step.output_key] = StepResult(
                    step_name=step.name,
                    status="awaiting_approval",
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
                state.status = "paused"
                self._paused_at[state.workflow_id] = i
                await self._persist(state)
                logger.info("Workflow paused at step '%s' for approval", step.name)
                return state

            if step.type == "agent":
                result = await self._execute_agent_step(step, state)
                state.steps[step.output_key] = result
                await self._persist(state)
                if result.status == "failed":
                    state.status = "failed"
                    await self._persist(state)
                    return state

        state.status = "completed"
        state.current_step = None
        state.updated_at = datetime.now(timezone.utc).isoformat()
        return state

    async def _execute_agent_step(
        self, step: WorkflowStepSpec, state: WorkflowState
    ) -> StepResult:
        """Execute a single agent step.

        Args:
            step: The step specification.
            state: Current workflow state (for template interpolation).

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

        logger.info("Executing step '%s' with agent '%s'", step.name, step.agent)

        try:
            client = self._client_factory(step.agent)
            response = await client.run(prompt)
        except Exception as exc:
            return StepResult(
                step_name=step.name, status="failed",
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        return StepResult(
            step_name=step.name,
            status="completed",
            output=response.output,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
