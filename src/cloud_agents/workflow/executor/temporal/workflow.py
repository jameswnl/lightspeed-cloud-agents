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
    from cloud_agents.workflow.security.advisory import AdvisoryEnforcer
    from cloud_agents.workflow.security.auto_approve import ApprovalPolicy, classify_step_risk
    from cloud_agents.workflow.core.conditions import evaluate_condition
    from cloud_agents.workflow.core.definition import WorkflowStepSpec
    from cloud_agents.workflow.core.execution import (
        activity_error_text,
        apply_one_step_defaults,
        chunk_parallel_groups,
        enforce_provider_boundary,
        normalize_workflow_step,
        resolve_sandbox_image,
        run_with_retries,
        workflow_defaults_from_definition,
    )
    from cloud_agents.workflow.core.interpolation import interpolate
    from cloud_agents.workflow.core.state import StepResult as LegacyStepResult
    from cloud_agents.workflow.core.state import WorkflowState
    from cloud_agents.workflow.core.models import (
        StepResult,
        StepTranscript,
        WorkflowEvent,
        WorkflowInput,
        WorkflowOutput,
        WorkflowStatus,
    )


def _activity_succeeded(result: Any) -> bool:
    """Check whether a sandbox activity result counts as success for retry.

    Only ``completed`` steps succeed; failed/denied/skipped results fall
    through to the transient-failure classifier shared with the local
    runner (issue #268).
    """
    if isinstance(result, dict):
        return result.get("status") == "completed"
    return getattr(result, "status", "") == "completed"


def _activity_error(result: Any) -> Optional[str]:
    """Extract error text from a sandbox activity result for classification."""
    if isinstance(result, dict):
        error = result.get("error")
        return error if isinstance(error, str) else (str(error) if error is not None else None)
    error = getattr(result, "error", None)
    return error if isinstance(error, str) else (str(error) if error is not None else None)


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

        # One-step convention (issue #268): a single step may omit
        # ``name``/``output_key``; default them before any step indexing
        # so the bare shorthand behaves like the local runner and never
        # KeyErrors downstream (#270).
        if len(steps) == 1:
            steps = [apply_one_step_defaults(dict(steps[0]), step_count=1)]

        # Group scheduling uses the canonical chunking helper (issue
        # #268): contiguous same-group steps run concurrently via
        # asyncio.gather; the group completes when all members complete,
        # and a failed/denied member stops subsequent steps (siblings
        # already running are not cancelled; no concurrency limit).
        for group, group_steps in chunk_parallel_groups(steps):
            if group:
                results = await asyncio.gather(
                    *[self._execute_step(s, input) for s in group_steps]
                )
                if any(r and r.status in ("failed", "denied") for r in results):
                    break
            else:
                result = await self._execute_step(group_steps[0], input)
                if result and result.status in ("failed", "denied"):
                    break

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

        # Unknown step types fail the step (#270): a silent None would
        # let the run report success with a missing output, diverging
        # from the local runner's rejection.
        result = StepResult(
            status="failed",
            error=(
                f"unknown step type {step['type']!r}: only 'agent' and "
                "'human-approval' are supported"
            ),
        )
        self._steps[output_key] = result
        self._emit("step.failed", step_name)
        return result

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

        # Interpolate approval message with step outputs
        raw_message = step.get("message", "")
        interpolated_message = self._interpolate_prompt(raw_message, input) if raw_message else raw_message

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
                        "message": interpolated_message,
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
        # Canonical normalization shared with the local runner (issue
        # #268): the definition's workflow-level defaults thread through
        # the same precedence chain, one-step definitions get the
        # agent/result convention, and invalid steps fail the step --
        # never the workflow. normalize_workflow_step is pure
        # (pydantic + pure helpers), so replay sees identical values.
        definition = input.definition
        spec = definition.get("spec", {})
        step_count = len(spec.get("steps", []))
        definition_defaults = workflow_defaults_from_definition(definition)
        try:
            normalized, meta = normalize_workflow_step(
                step,
                workflow_defaults=definition_defaults,
                step_count=step_count,
            )
            enforce_provider_boundary(
                normalized.inference_provider,
                input.provider.model_dump(),
            )
        except ValueError as exc:
            failed_name = step.get("name", "agent")
            failed_key = step.get("output_key", "result")
            result = StepResult(status="failed", error=str(exc))
            self._steps[failed_key] = result
            self._step_transcripts[failed_key] = StepTranscript(
                step_name=failed_name,
            ).model_dump()
            self._emit("step.failed", failed_name)
            return result
        step_name = normalized.name
        output_key = normalized.output_key
        timeout_seconds = normalized.timeout_seconds or 600
        max_retries = normalized.max_retries
        # Sandbox-image precedence (issue #268): step spawn_config →
        # definition spawn_config → run-level input image. The run-level
        # image is a default the definition layer outranks.
        sandbox_image = resolve_sandbox_image(
            normalized.spawn_config,
            definition_defaults,
            input.sandbox_image,
        )
        if enforcer is None:
            enforcer = AdvisoryEnforcer(enabled=False)

        # Provider selection (issue #268): the normalized spec already
        # carries the step/definition winner (the run-level provider is
        # the default only when neither layer set one). The run-level
        # credential reference is honored solely for the same provider:
        # otherwise the winner would silently bind another provider's
        # credentials (cross-provider confusion). Runtime credentials
        # still resolve outside serializable data (pre-#269 contract).
        activity_provider = input.provider.model_dump()
        if normalized.inference_provider is not None:
            activity_provider["name"] = normalized.inference_provider.name
            activity_provider["model"] = normalized.inference_provider.model
            if normalized.inference_provider.name != input.provider.name:
                activity_provider.pop("credentials_secret", None)
            elif not activity_provider.get("credentials_secret"):
                definition_provider = definition_defaults.get("provider") or {}
                if isinstance(definition_provider, dict):
                    reference = definition_provider.get("credentials_secret")
                    if reference:
                        activity_provider["credentials_secret"] = reference

        # Canonical dispatch (#270): the activity receives the NORMALIZED
        # step -- workflow-default inheritance (spawn, mcp_servers,
        # allowed_skills, permissions, timeout), permission collapse, and
        # the one-step naming convention all materialized -- never the
        # raw definition step. Pure dict ops on immutable inputs keep
        # Temporal replay deterministic.
        resolved_step = normalized.model_dump(exclude_none=True)
        resolved_step.update({k: v for k, v in meta.items() if v is not None})
        if prompt := normalized.prompt:
            interpolated = self._interpolate_prompt(prompt, input)
            resolved_step["prompt"] = enforcer.annotate_prompt(interpolated)
        if input.advisory:
            resolved_step["advisory"] = True

        self._emit("step.started", step_name)

        attempt_number = 0

        async def attempt() -> Any:
            """Run one sandbox activity attempt without activity-level retries.

            Retry accounting lives in run_with_retries (shared with the
            local runner) so a single policy covers exceptions and
            transient result failures; the activity itself tries once.
            """
            nonlocal attempt_number
            attempt_number += 1
            return await workflow.execute_activity(
                "run_sandbox_step",
                args=[
                    {
                        "step": resolved_step,
                        "workflow_id": input.workflow_id,
                        "provider": activity_provider,
                        "sandbox_image": sandbox_image,
                        "execution_context": dict(normalized.context),
                        "attempt": attempt_number,
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
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

        try:
            result = await run_with_retries(
                attempt,
                max_retries,
                _activity_succeeded,
                _activity_error,
                # Classify by root cause: str(ActivityError) names only
                # the activity/retry state, so transient infra failures
                # (e.g. sandbox 502s) would otherwise never retry here
                # while the local runner retries them.
                exc_text=activity_error_text,
                retry_sleep=workflow.sleep if workflow.in_workflow() else None,
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

        except ActivityError as exc:
            error_detail = activity_error_text(exc)
            error = "retries exhausted"
            if error_detail:
                error = f"{error}: {error_detail}"
            step_result = StepResult(status="failed", error=error)
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
