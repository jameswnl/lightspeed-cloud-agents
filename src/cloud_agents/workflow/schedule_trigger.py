"""Schedule trigger — cron-based workflow execution via Temporal Schedules API.

Provides REST endpoints for creating, listing, viewing, pausing, resuming,
and deleting Temporal Schedules that trigger workflow executions on a cron
schedule. Each schedule references a workflow definition by name from the
definition store and reconstructs a full WorkflowInput on each trigger.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.definition_store import DefinitionStore
from cloud_agents.workflow.temporal_metrics import ls_schedule_triggers_total
from cloud_agents.workflow.temporal_models import ProviderConfig, WorkflowInput
from cloud_agents.workflow.temporal_worker import DEFAULT_TASK_QUEUE
from cloud_agents.workflow.temporal_workflow import AgentWorkflow

logger = logging.getLogger(__name__)

# Regex for standard 5-field cron expressions.
# Allows digits, *, /, -, comma in each of the 5 fields.
_CRON_FIELD = r"[\d\*\/\-\,]+"
_CRON_5_FIELD_RE = re.compile(
    rf"^\s*{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s*$"
)

# Temporal-supported shorthands.
_CRON_SHORTHANDS = frozenset({
    "@yearly",
    "@annually",
    "@monthly",
    "@weekly",
    "@daily",
    "@midnight",
    "@hourly",
    "@every",
})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ScheduleSpec(BaseModel):
    """Cron schedule specification.

    Attributes:
        cron: Cron expression (5-field standard or Temporal shorthand).
        timezone: IANA timezone name for schedule evaluation.
        jitter_seconds: Random jitter in seconds to avoid thundering herd.
        overlap_policy: What to do when a new run overlaps a running one.
    """

    cron: str
    timezone: str = "UTC"
    jitter_seconds: int = 0
    overlap_policy: str = "skip"

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        """Validate cron expression format.

        Accepts standard 5-field cron expressions and Temporal shorthand
        keywords (@daily, @hourly, etc.). Rejects empty strings, garbage,
        and 6-field (seconds) expressions.

        Parameters:
            v: The cron expression string.

        Returns:
            The validated cron expression.

        Raises:
            ValueError: When the cron expression is invalid.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("Cron expression must not be empty")

        # Check for Temporal shorthands
        if stripped.startswith("@"):
            # Accept @every with an argument too
            keyword = stripped.split()[0].lower()
            if keyword in _CRON_SHORTHANDS:
                return stripped
            raise ValueError(
                f"Unknown cron shorthand '{stripped}'. "
                f"Supported: {', '.join(sorted(_CRON_SHORTHANDS))}"
            )

        # Must be exactly 5 fields
        fields = stripped.split()
        if len(fields) != 5:
            raise ValueError(
                f"Expected 5-field cron expression (minute hour day month weekday), "
                f"got {len(fields)} fields: '{stripped}'"
            )

        if not _CRON_5_FIELD_RE.match(stripped):
            raise ValueError(
                f"Invalid cron expression: '{stripped}'. "
                f"Each field must contain digits, *, /, -, or commas."
            )

        return stripped


class ScheduleInput(BaseModel):
    """Input for creating a new schedule.

    Attributes:
        schedule_id: Unique identifier for the schedule. Auto-generated if not provided.
        workflow_name: Name of a workflow definition in the store.
        schedule: Cron schedule specification.
        provider: LLM provider configuration. Falls back to definition default.
        sandbox_image: Container image for sandbox pods.
        input_prompt: Optional input prompt for each workflow run.
        paused: Whether to create the schedule in paused state.
    """

    schedule_id: str | None = None
    workflow_name: str
    schedule: ScheduleSpec
    provider: ProviderConfig | None = None
    sandbox_image: str = "quay.io/openshift-lightspeed/lightspeed-agentic-sandbox:latest"
    input_prompt: str | None = None
    paused: bool = False

    def __init__(self, **data: Any) -> None:
        """Initialize ScheduleInput with auto-generated ID if not provided."""
        super().__init__(**data)
        if self.schedule_id is None:
            self.schedule_id = f"sched-{uuid.uuid4().hex[:12]}"


class ScheduleInfo(BaseModel):
    """Information about an existing schedule.

    Attributes:
        schedule_id: Unique schedule identifier.
        workflow_name: Workflow definition name.
        cron: Cron expression.
        timezone: IANA timezone.
        paused: Whether the schedule is paused.
        next_run: ISO 8601 timestamp of next scheduled run, if known.
        last_run: ISO 8601 timestamp of last run, if any.
        overlap_policy: Overlap policy name.
    """

    schedule_id: str
    workflow_name: str
    cron: str
    timezone: str = "UTC"
    paused: bool = False
    next_run: str | None = None
    last_run: str | None = None
    overlap_policy: str = "skip"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_schedule_router(
    temporal_client: Any,
    definition_store: DefinitionStore,
    auth_dependency: Optional[Any] = None,
) -> APIRouter:
    """Build FastAPI router for schedule CRUD endpoints.

    Parameters:
        temporal_client: Connected Temporal client instance.
        definition_store: Store for workflow definition lookup.
        auth_dependency: Optional FastAPI auth dependency.

    Returns:
        APIRouter with schedule endpoints under /v1/schedules.
    """
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec as TemporalScheduleSpec,
        ScheduleState,
    )

    from cloud_agents.workflow.authorization import WorkflowAuthzContext

    dependencies = [Depends(auth_dependency)] if auth_dependency else []
    router = APIRouter(
        prefix="/v1/schedules",
        tags=["schedules"],
        dependencies=dependencies,
    )

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_schedule(
        schedule_input: ScheduleInput,
    ) -> dict[str, str]:
        """Create a new cron schedule for a workflow.

        Looks up the workflow definition by name, builds a WorkflowInput,
        and creates a Temporal Schedule.

        Parameters:
            schedule_input: Schedule creation request.

        Returns:
            Dict with the schedule_id.

        Raises:
            HTTPException: 404 if workflow not found, 400 if no provider,
                409 if schedule_id already exists.
        """
        # Look up workflow definition
        stored = await definition_store.get(schedule_input.workflow_name)
        if not stored:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{schedule_input.workflow_name}' not found",
            )

        # Resolve provider: request > definition > error
        provider = schedule_input.provider
        if not provider and stored.definition.provider:
            provider = ProviderConfig(**stored.definition.provider.model_dump())
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No provider configured for workflow '{schedule_input.workflow_name}' "
                f"and none provided in request",
            )

        schedule_id = schedule_input.schedule_id
        schedule_spec = schedule_input.schedule

        # Build workflow input that will be used on each trigger
        definition = stored.definition.model_dump()
        authz_ctx = WorkflowAuthzContext(
            owner_username="scheduler",
            owner_groups=[],
            workflow_name=schedule_input.workflow_name,
        )

        workflow_input = WorkflowInput(
            definition=definition,
            input_prompt=schedule_input.input_prompt,
            workflow_id=f"sched-{schedule_id}-placeholder",
            provider=provider,
            sandbox_image=schedule_input.sandbox_image,
            authz_context=authz_ctx,
        )

        # Map overlap policy string to Temporal enum
        overlap_map = {
            "skip": ScheduleOverlapPolicy.SKIP,
            "buffer_one": ScheduleOverlapPolicy.BUFFER_ONE,
            "cancel_other": ScheduleOverlapPolicy.CANCEL_OTHER,
            "allow_all": ScheduleOverlapPolicy.ALLOW_ALL,
        }
        overlap = overlap_map.get(
            schedule_spec.overlap_policy, ScheduleOverlapPolicy.SKIP
        )

        try:
            await temporal_client.create_schedule(
                id=schedule_id,
                schedule=Schedule(
                    action=ScheduleActionStartWorkflow(
                        AgentWorkflow.run,
                        args=[workflow_input],
                        id=f"sched-{schedule_id}-{{{{workflow.now}}}}",
                        task_queue=DEFAULT_TASK_QUEUE,
                    ),
                    spec=TemporalScheduleSpec(
                        cron_expressions=[schedule_spec.cron],
                        jitter=timedelta(seconds=schedule_spec.jitter_seconds),
                    ),
                    policy=SchedulePolicy(overlap=overlap),
                    state=ScheduleState(paused=schedule_input.paused),
                ),
                trigger_immediately=False,
                memo={"workflow_name": schedule_input.workflow_name},
            )
        except Exception as exc:
            from temporalio.service import RPCError, RPCStatusCode

            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.ALREADY_EXISTS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Schedule '{schedule_id}' already exists",
                ) from exc
            logger.error("Failed to create schedule '%s': %s", schedule_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal error creating schedule",
            ) from None

        ls_schedule_triggers_total.labels(
            workflow_name=schedule_input.workflow_name, status="created"
        ).inc()

        emit_audit(
            event_type="schedule_created",
            workflow_id="",
            details={
                "schedule_id": schedule_id,
                "workflow_name": schedule_input.workflow_name,
                "cron": schedule_spec.cron,
                "timezone": schedule_spec.timezone,
                "paused": schedule_input.paused,
            },
        )

        return {"schedule_id": schedule_id}

    @router.get("")
    async def list_schedules() -> list[dict[str, Any]]:
        """List all schedules.

        Iterates Temporal schedules and returns schedule information.

        Returns:
            List of schedule info dicts.
        """
        results: list[dict[str, Any]] = []
        async for entry in await temporal_client.list_schedules():
            workflow_name = ""
            if hasattr(entry, "memo") and isinstance(entry.memo, dict):
                workflow_name = entry.memo.get("workflow_name", "")

            cron = ""
            if (
                hasattr(entry, "spec")
                and hasattr(entry.spec, "spec")
                and entry.spec.spec.cron_expressions
            ):
                cron = entry.spec.spec.cron_expressions[0]

            timezone = ""
            if hasattr(entry, "spec") and hasattr(entry.spec, "spec"):
                timezone = getattr(entry.spec.spec, "timezone_name", "UTC") or "UTC"

            paused = False
            if hasattr(entry, "state"):
                paused = getattr(entry.state, "paused", False)

            next_run = None
            if hasattr(entry, "info") and entry.info.next_action_times:
                next_run = str(entry.info.next_action_times[0])

            last_run = None
            if hasattr(entry, "info") and entry.info.recent_actions:
                last_run = str(entry.info.recent_actions[-1])

            overlap_policy = "SKIP"
            if hasattr(entry, "policy") and hasattr(entry.policy, "overlap"):
                overlap_policy = getattr(entry.policy.overlap, "name", "SKIP")

            results.append(
                ScheduleInfo(
                    schedule_id=entry.id,
                    workflow_name=workflow_name,
                    cron=cron,
                    timezone=timezone,
                    paused=paused,
                    next_run=next_run,
                    last_run=last_run,
                    overlap_policy=overlap_policy.lower(),
                ).model_dump()
            )
        return results

    @router.get("/{schedule_id}")
    async def get_schedule(schedule_id: str) -> dict[str, Any]:
        """Get details of a specific schedule.

        Parameters:
            schedule_id: The schedule identifier.

        Returns:
            Schedule info dict.

        Raises:
            HTTPException: 404 if schedule not found.
        """
        try:
            handle = temporal_client.get_schedule_handle(schedule_id)
            desc = await handle.describe()
        except Exception as exc:
            from temporalio.service import RPCError, RPCStatusCode

            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Schedule '{schedule_id}' not found",
                ) from exc
            raise

        workflow_name = ""
        if hasattr(desc, "memo") and isinstance(desc.memo, dict):
            workflow_name = desc.memo.get("workflow_name", "")

        cron = ""
        if desc.schedule.spec.cron_expressions:
            cron = desc.schedule.spec.cron_expressions[0]

        timezone = getattr(desc.schedule.spec, "timezone_name", "UTC") or "UTC"
        paused = getattr(desc.schedule.state, "paused", False)

        next_run = None
        if desc.info.next_action_times:
            next_run = str(desc.info.next_action_times[0])

        last_run = None
        if desc.info.recent_actions:
            last_run = str(desc.info.recent_actions[-1])

        overlap_policy = "SKIP"
        if hasattr(desc.schedule.policy, "overlap"):
            overlap_policy = getattr(desc.schedule.policy.overlap, "name", "SKIP")

        return ScheduleInfo(
            schedule_id=schedule_id,
            workflow_name=workflow_name,
            cron=cron,
            timezone=timezone,
            paused=paused,
            next_run=next_run,
            last_run=last_run,
            overlap_policy=overlap_policy.lower(),
        ).model_dump()

    @router.delete("/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict[str, str]:
        """Delete a schedule.

        Parameters:
            schedule_id: The schedule identifier.

        Returns:
            Status dict.

        Raises:
            HTTPException: 404 if schedule not found.
        """
        try:
            handle = temporal_client.get_schedule_handle(schedule_id)
            await handle.delete()
        except Exception as exc:
            from temporalio.service import RPCError, RPCStatusCode

            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Schedule '{schedule_id}' not found",
                ) from exc
            raise

        ls_schedule_triggers_total.labels(
            workflow_name="unknown", status="deleted"
        ).inc()

        emit_audit(
            event_type="schedule_deleted",
            workflow_id="",
            details={"schedule_id": schedule_id},
        )

        return {"status": "deleted"}

    @router.post("/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str) -> dict[str, str]:
        """Pause a schedule.

        Parameters:
            schedule_id: The schedule identifier.

        Returns:
            Status dict.

        Raises:
            HTTPException: 404 if schedule not found.
        """
        try:
            handle = temporal_client.get_schedule_handle(schedule_id)
            await handle.pause()
        except Exception as exc:
            from temporalio.service import RPCError, RPCStatusCode

            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Schedule '{schedule_id}' not found",
                ) from exc
            raise

        ls_schedule_triggers_total.labels(
            workflow_name="unknown", status="paused"
        ).inc()

        return {"status": "paused"}

    @router.post("/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str) -> dict[str, str]:
        """Resume a paused schedule.

        Parameters:
            schedule_id: The schedule identifier.

        Returns:
            Status dict.

        Raises:
            HTTPException: 404 if schedule not found.
        """
        try:
            handle = temporal_client.get_schedule_handle(schedule_id)
            await handle.unpause()
        except Exception as exc:
            from temporalio.service import RPCError, RPCStatusCode

            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Schedule '{schedule_id}' not found",
                ) from exc
            raise

        ls_schedule_triggers_total.labels(
            workflow_name="unknown", status="resumed"
        ).inc()

        return {"status": "resumed"}

    return router
