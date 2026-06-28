"""Generic Temporal workflow for agent orchestration.

A single AgentWorkflow class interprets any workflow YAML at runtime.
Registered once at worker startup — new workflow definitions don't
require worker restarts.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from agents.workflow.temporal_models import (
        StepResult,
        WorkflowEvent,
        WorkflowInput,
        WorkflowOutput,
        WorkflowStatus,
    )


@workflow.defn
class AgentWorkflow:
    """Interprets any workflow YAML at runtime."""

    def __init__(self) -> None:
        """Initialize workflow state."""
        self._steps: dict[str, StepResult] = {}
        self._approval_decisions: dict[str, dict[str, Any]] = {}
        self._events: list[WorkflowEvent] = []

    @workflow.signal
    async def approve(
        self, step_name: str, decision: str, selected_option_id: Optional[str] = None,
    ) -> None:
        """Receive an approval decision for a step."""
        self._approval_decisions[step_name] = {
            "decision": decision,
            "selected_option_id": selected_option_id,
        }

    @workflow.query
    def get_status(self) -> WorkflowStatus:
        """Return current workflow status for queries."""
        return WorkflowStatus(steps=self._steps, events=self._events)

    @workflow.run
    async def run(self, input: WorkflowInput) -> WorkflowOutput:
        """Execute the workflow by interpreting the YAML definition."""
        definition = input.definition
        steps = definition.get("spec", {}).get("steps", [])

        for step in steps:
            result = await self._execute_step(step, input)
            if result and result.status in ("failed", "denied"):
                break

        return WorkflowOutput(steps=self._steps)

    async def _execute_step(
        self, step: dict[str, Any], input: WorkflowInput,
    ) -> Optional[StepResult]:
        """Execute a single step with condition evaluation."""
        step_name = step["name"]
        output_key = step["output_key"]

        if condition := step.get("condition"):
            from agents.workflow.conditions import evaluate_condition
            if not evaluate_condition(condition, self._steps):
                self._steps[output_key] = StepResult(status="skipped")
                self._emit("step.skipped", step_name)
                return None

        if step["type"] == "human-approval":
            return await self._handle_approval(step)

        if step["type"] == "agent":
            return await self._handle_agent_step(step, input)

        return None

    async def _handle_approval(self, step: dict[str, Any]) -> StepResult:
        """Handle a human-approval step with signal + timeout."""
        step_name = step["name"]
        output_key = step["output_key"]
        timeout_seconds = step.get("timeout_seconds", 86400)

        self._emit("workflow.paused", step_name)

        try:
            await workflow.wait_condition(
                lambda: step_name in self._approval_decisions,
                timeout=timedelta(seconds=timeout_seconds),
            )
        except asyncio.TimeoutError:
            result = StepResult(
                status="denied",
                output={"approved": False, "reason": "timeout"},
            )
            self._steps[output_key] = result
            self._emit("step.denied", step_name)
            return result

        decision_data = self._approval_decisions[step_name]
        approved = decision_data["decision"] == "approved"
        result = StepResult(
            status="completed" if approved else "denied",
            output={
                "approved": approved,
                "selected_option_id": decision_data.get("selected_option_id"),
            },
        )
        self._steps[output_key] = result
        self._emit("step.completed" if approved else "step.denied", step_name)
        return result

    async def _handle_agent_step(
        self, step: dict[str, Any], input: WorkflowInput,
    ) -> StepResult:
        """Handle an agent step by dispatching to the sandbox activity."""
        step_name = step["name"]
        output_key = step["output_key"]
        timeout_seconds = step.get("timeout_seconds", 600)
        max_retries = step.get("max_retries", 1)

        self._emit("step.started", step_name)

        try:
            result = await workflow.execute_activity(
                "run_sandbox_step",
                args=[{
                    "step": step,
                    "workflow_id": input.workflow_id,
                    "provider": input.provider.model_dump(),
                    "sandbox_image": input.sandbox_image,
                    "skills_image": input.skills_image,
                    "skills_paths": input.skills_paths,
                    "context": {k: v.model_dump() for k, v in self._steps.items()},
                }],
                start_to_close_timeout=timedelta(seconds=timeout_seconds),
                retry_policy=RetryPolicy(maximum_attempts=max_retries + 1),
            )

            step_result = StepResult(**result) if isinstance(result, dict) else result

        except ActivityError:
            step_result = StepResult(status="failed", error="retries exhausted")
            self._steps[output_key] = step_result
            self._emit("step.failed", step_name)

            escalation = await workflow.execute_activity(
                "build_escalation_activity",
                args=[{k: v.model_dump() for k, v in self._steps.items()}],
                start_to_close_timeout=timedelta(seconds=60),
            )
            self._steps["escalation"] = StepResult(**escalation) if isinstance(escalation, dict) else escalation
            return step_result

        self._steps[output_key] = step_result
        event_type = "step.completed" if step_result.status == "completed" else "step.failed"
        self._emit(event_type, step_name)
        return step_result

    def _emit(self, event_type: str, step_name: str) -> None:
        """Emit a workflow event."""
        self._events.append(WorkflowEvent(
            type=event_type,
            step=step_name,
            timestamp=workflow.now().isoformat(),
        ))
