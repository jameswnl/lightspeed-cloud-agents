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
    from cloud_agents.workflow.advisory import AdvisoryEnforcer
    from cloud_agents.workflow.auto_approve import ApprovalPolicy, classify_step_risk
    from cloud_agents.workflow.conditions import evaluate_condition
    from cloud_agents.workflow.definition import WorkflowStepSpec
    from cloud_agents.workflow.interpolation import interpolate
    from cloud_agents.workflow.state import StepResult as LegacyStepResult
    from cloud_agents.workflow.state import WorkflowState
    from cloud_agents.workflow.temporal_models import (
        StepResult,
        StepTranscript,
        WorkflowEvent,
        WorkflowInput,
        WorkflowOutput,
        WorkflowStatus,
    )


@workflow.defn(sandboxed=False)
class AgentWorkflow:
    """Interprets any workflow YAML at runtime."""

    def __init__(self) -> None:
        """Initialize workflow state."""
        self._steps: dict[str, StepResult] = {}
        self._approval_decisions: dict[str, dict[str, Any]] = {}
        self._events: list[WorkflowEvent] = []
        self._authz_context: Optional[dict[str, Any]] = None
        self._workflow_context: Optional[dict[str, Any]] = None
        self._step_transcripts: dict[str, dict[str, Any]] = {}
        self._session_results: dict[str, dict[str, Any]] = {}

    @workflow.signal
    async def session_result(
        self,
        session_id: str,
        result_data: dict[str, Any],
    ) -> None:
        """Receive output from a CLI session.

        Parameters:
            session_id: The CLI session identifier.
            result_data: Result data from the session.
        """
        self._session_results[session_id] = result_data

    @workflow.signal
    async def approve(
        self,
        step_name: str,
        decision: str,
        selected_option_id: Optional[str] = None,
        approver_username: Optional[str] = None,
        approver_uid: Optional[str] = None,
    ) -> None:
        """Receive an approval decision for a step."""
        self._approval_decisions[step_name] = {
            "decision": decision,
            "selected_option_id": selected_option_id,
            "approver_username": approver_username,
            "approver_uid": approver_uid,
        }

    @workflow.query
    def get_status(self) -> WorkflowStatus:
        """Return current workflow status for queries."""
        return WorkflowStatus(steps=self._steps, events=self._events)

    @workflow.query
    def get_authz_context(self) -> dict[str, Any] | None:
        """Return the workflow's authorization context."""
        return self._authz_context

    @workflow.query
    def get_step_transcripts(self) -> dict[str, dict[str, Any]]:
        """Return stored step transcripts keyed by output_key.

        Returns:
            Dict mapping output_key to truncated transcript dicts.
        """
        return dict(self._step_transcripts)

    @workflow.query
    def get_session_results(self) -> dict[str, dict[str, Any]]:
        """Return stored CLI session results.

        Returns:
            Dict mapping session_id to result data dicts.
        """
        return dict(self._session_results)

    @workflow.query
    def get_workflow_context(self) -> dict[str, Any] | None:
        """Return the workflow's definition, input prompt, and provider context.

        Returns:
            Dict with definition, input_prompt, provider_name, provider_model,
            or None if the workflow hasn't started yet.
        """
        return self._workflow_context

    @workflow.run
    async def run(self, input: WorkflowInput) -> WorkflowOutput:
        """Execute the workflow by interpreting the YAML definition."""
        if input.authz_context:
            self._authz_context = input.authz_context.model_dump()

        self._workflow_context = {
            "definition": input.definition,
            "input_prompt": input.input_prompt,
            "provider_name": input.provider.name,
            "provider_model": input.provider.model,
        }

        definition = input.definition
        steps = definition.get("spec", {}).get("steps", [])

        i = 0
        while i < len(steps):
            step = steps[i]
            group = step.get("parallel_group")

            if group:
                group_steps = []
                while i < len(steps) and steps[i].get("parallel_group") == group:
                    group_steps.append(steps[i])
                    i += 1
                results = await asyncio.gather(
                    *[self._execute_step(s, input) for s in group_steps]
                )
                if any(r and r.status in ("failed", "denied") for r in results):
                    break
            else:
                result = await self._execute_step(step, input)
                if result and result.status in ("failed", "denied"):
                    break
                i += 1

        return WorkflowOutput(steps=self._steps)

    async def _execute_step(
        self,
        step: dict[str, Any],
        input: WorkflowInput,
    ) -> Optional[StepResult]:
        """Execute a single step with condition evaluation."""
        step_name = step["name"]
        output_key = step["output_key"]
        enforcer = AdvisoryEnforcer(enabled=input.advisory)

        if condition := step.get("condition"):
            if not self._evaluate_condition(condition):
                self._steps[output_key] = StepResult(status="skipped")
                self._emit("step.skipped", step_name)
                return None

        if step["type"] == "human-approval":
            if enforcer.should_skip_approval():
                result = StepResult(
                    status="completed",
                    output={"approved": True, "advisory": True},
                )
                self._steps[output_key] = result
                self._emit("step.advisory_skipped", step_name)
                return result
            return await self._handle_approval(step, input)

        if step["type"] == "agent":
            return await self._handle_agent_step(step, input, enforcer)

        return None

    async def _handle_approval(
        self,
        step: dict[str, Any],
        input: WorkflowInput,
    ) -> StepResult:
        """Handle a human-approval step with auto-approve check + signal."""
        step_name = step["name"]
        output_key = step["output_key"]
        timeout_seconds = step.get("timeout_seconds", 86400)

        policy_dict = input.approval_policy or {}
        policy = ApprovalPolicy(**policy_dict)
        step_spec = WorkflowStepSpec(
            name=step_name,
            type=step["type"],
            output_key=output_key,
            risk_level=step.get("risk_level"),
            message=step.get("message"),
        )
        classification = classify_step_risk(step_spec, policy)

        if classification.auto_approved:
            result = StepResult(
                status="completed",
                output={"approved": True, "auto_approved": True},
            )
            self._steps[output_key] = result
            self._emit("step.auto_approved", step_name)
            return result

        self._emit("workflow.paused", step_name)

        try:
            await workflow.execute_activity(
                "send_approval_notification",
                args=[
                    {
                        "workflow_id": input.workflow_id,
                        "step_name": step_name,
                        "message": step.get("message", ""),
                        "notifier_config": input.notifier_config,
                    }
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:
            pass

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
                "approver_username": decision_data.get("approver_username"),
                "approver_uid": decision_data.get("approver_uid"),
            },
        )
        self._steps[output_key] = result
        self._emit("step.completed" if approved else "step.denied", step_name)
        return result

    async def _handle_agent_step(
        self,
        step: dict[str, Any],
        input: WorkflowInput,
        enforcer: Optional[AdvisoryEnforcer] = None,
    ) -> StepResult:
        """Handle an agent step by dispatching to the sandbox activity."""
        step_name = step["name"]
        output_key = step["output_key"]
        timeout_seconds = step.get("timeout_seconds", 600)
        max_retries = step.get("max_retries", 1)
        if enforcer is None:
            enforcer = AdvisoryEnforcer(enabled=False)

        resolved_step = dict(step)
        if prompt := step.get("prompt"):
            interpolated = self._interpolate_prompt(prompt, input)
            resolved_step["prompt"] = enforcer.annotate_prompt(interpolated)
        if input.advisory:
            resolved_step["advisory"] = True

        self._emit("step.started", step_name)

        try:
            result = await workflow.execute_activity(
                "run_sandbox_step",
                args=[
                    {
                        "step": resolved_step,
                        "workflow_id": input.workflow_id,
                        "provider": input.provider.model_dump(),
                        "sandbox_image": input.sandbox_image,
                        "skills_image": input.skills_image,
                        "skills_paths": input.skills_paths,
                        "mcp_servers": (
                            [s.model_dump() for s in input.mcp_servers]
                            if input.mcp_servers
                            else None
                        ),
                        "context": {k: v.model_dump() for k, v in self._steps.items()},
                    }
                ],
                start_to_close_timeout=timedelta(seconds=timeout_seconds),
                heartbeat_timeout=timedelta(seconds=180),
                retry_policy=RetryPolicy(maximum_attempts=max_retries + 1),
            )

            if isinstance(result, dict):
                # Extract and store transcript before constructing StepResult
                transcript_data = result.pop("transcript", None)
                step_result = StepResult(**result)
                if transcript_data:
                    transcript = StepTranscript(**transcript_data)
                    self._step_transcripts[output_key] = transcript.truncate(
                        max_events=50,
                    ).model_dump()
                else:
                    self._step_transcripts[output_key] = StepTranscript(
                        step_name=step_name,
                    ).model_dump()
            else:
                step_result = result
                self._step_transcripts[output_key] = StepTranscript(
                    step_name=step_name,
                ).model_dump()
            if enforcer.enabled and step_result.output:
                step_result = StepResult(
                    status=step_result.status,
                    output=enforcer.annotate_output(step_result.output),
                    error=step_result.error,
                )

        except ActivityError:
            step_result = StepResult(status="failed", error="retries exhausted")
            self._steps[output_key] = step_result
            self._step_transcripts[output_key] = StepTranscript(
                step_name=step_name,
            ).model_dump()
            self._emit("step.failed", step_name)

            escalation = await workflow.execute_activity(
                "build_escalation_activity",
                args=[
                    {k: v.model_dump() for k, v in self._steps.items()},
                    input.definition.get("metadata", {}).get("name", "workflow"),
                    input.escalation_config,
                    input.definition,
                    input.input_prompt,
                    [e.model_dump() for e in self._events],
                    input.provider.name,
                    input.workflow_id,
                ],
                start_to_close_timeout=timedelta(seconds=60),
            )
            self._steps["escalation"] = (
                StepResult(**escalation) if isinstance(escalation, dict) else escalation
            )
            self._emit("workflow.escalated", step_name)
            return step_result

        self._steps[output_key] = step_result
        event_type = (
            "step.completed" if step_result.status == "completed" else "step.failed"
        )
        self._emit(event_type, step_name)
        return step_result

    def _build_workflow_state(self) -> WorkflowState:
        """Build a WorkflowState from current Temporal step results."""
        status_map = {"denied": "failed", "escalated": "failed"}
        legacy_steps = {
            k: LegacyStepResult(
                step_name=k,
                status=status_map.get(v.status, v.status),
                output=v.output,
            )
            for k, v in self._steps.items()
        }
        return WorkflowState(
            workflow_id="eval",
            workflow_name="eval",
            created_at="",
            updated_at="",
            steps=legacy_steps,
        )

    def _evaluate_condition(self, condition: str) -> bool:
        """Evaluate a step condition using the shared safe evaluator.

        Fails closed: unparseable conditions return False.
        """
        try:
            return evaluate_condition(condition, self._build_workflow_state())
        except ValueError:
            return False

    def _interpolate_prompt(self, template: str, input: WorkflowInput) -> str:
        """Interpolate prompt template with step outputs and input_prompt."""
        if input.input_prompt and "{{ input }}" in template:
            template = template.replace("{{ input }}", input.input_prompt)
        if "{{" not in template:
            return template
        try:
            return interpolate(template, self._build_workflow_state())
        except ValueError:
            return template

    def _emit(self, event_type: str, step_name: str) -> None:
        """Emit a workflow event."""
        self._events.append(
            WorkflowEvent(
                type=event_type,
                step=step_name,
                timestamp=workflow.now().isoformat(),
            )
        )
