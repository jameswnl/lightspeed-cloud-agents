"""Workflow advancement and recovery for stateless execution.

Handles callback-triggered advancement and background recovery
polling for orphaned steps. Uses optimistic locking to prevent
duplicate processing across replicas.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

# Any is used for spawner type hint to avoid circular imports

from agents.workflow.persistence import WorkflowPersistence
from agents.workflow.state import StepResult, StepResultPayload, WorkflowState

logger = logging.getLogger(__name__)

RECOVERY_POLL_INTERVAL = 60


class StaleStateError(Exception):
    """Raised when optimistic lock fails due to version mismatch."""


class IngestError(Exception):
    """Raised when step result ingestion fails validation."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        """Initialize with message and HTTP status code.

        Args:
            message: Error description.
            status_code: Suggested HTTP status code.
        """
        super().__init__(message)
        self.status_code = status_code


async def ingest_step_result(
    persistence: WorkflowPersistence,
    workflow_id: str,
    step_name: str,
    payload: StepResultPayload,
) -> WorkflowState:
    """Persist a completed/failed step result with CAS and idempotency.

    Shared by the callback endpoint and the recovery poller.

    Args:
        persistence: The persistence backend.
        workflow_id: Target workflow.
        step_name: Target step (matches output_key in definition).
        payload: The step result from the agent pod or poller.

    Returns:
        Updated WorkflowState after ingestion.

    Raises:
        IngestError: If the workflow/step is not found, attempt is stale,
            or the step is already terminal.
    """
    state = await persistence.load(workflow_id)
    if state is None:
        raise IngestError(f"Workflow '{workflow_id}' not found", status_code=404)

    step_result = state.steps.get(step_name)
    if step_result is None:
        raise IngestError(f"Step '{step_name}' not found in workflow", status_code=404)

    if step_result.status in ("completed", "failed"):
        current_attempt = (step_result.output or {}).get("attempt", 0)
        if payload.attempt == current_attempt and step_result.status == payload.status:
            return state
        raise IngestError(
            f"Step '{step_name}' is already terminal ({step_result.status})",
        )

    if step_result.status not in ("dispatched", "pending"):
        raise IngestError(
            f"Step '{step_name}' is in '{step_result.status}' status, "
            f"expected 'dispatched' or 'pending'",
        )

    current_attempt = (step_result.output or {}).get("attempt", 0)
    if payload.attempt < current_attempt:
        raise IngestError(
            f"Stale attempt {payload.attempt} for step '{step_name}' "
            f"(current attempt is {current_attempt})",
        )

    step_result.status = payload.status
    if payload.output is not None:
        step_result.output = {**(step_result.output or {}), **payload.output}
    if payload.error is not None:
        step_result.error = payload.error
    step_result.completed_at = payload.completed_at

    state.status = WorkflowState.derive_status(state.steps)
    expected_version = state.version
    await save_with_version(persistence, state, expected_version)
    return state


async def advance_workflow(
    persistence: WorkflowPersistence,
    dispatcher: Any,
    workflow_id: str,
    max_retries: int = 3,
) -> None:
    """Evaluate the next step and dispatch it.

    Runs after ingest_step_result() has persisted a completed/failed step.
    Uses CAS with retry for multi-replica safety.

    Args:
        persistence: The persistence backend.
        dispatcher: StepDispatcher for async dispatch (can be None).
        workflow_id: The workflow to advance.
        max_retries: Max CAS retry attempts on StaleStateError.
    """
    for attempt in range(max_retries):
        state = await persistence.load(workflow_id)
        if state is None:
            return

        if state.status in ("completed", "failed"):
            return

        if not state.definition_snapshot:
            logger.warning("Workflow %s has no definition_snapshot, cannot advance", workflow_id)
            return

        from agents.workflow.definition import WorkflowDefinition
        defn = WorkflowDefinition.model_validate(state.definition_snapshot)
        steps = defn.spec.steps

        next_step = None
        for step in steps:
            step_result = state.steps.get(step.output_key)
            if step_result is None or step_result.status == "pending":
                next_step = step
                break
            if step_result.status in ("dispatched", "running", "awaiting_approval"):
                return
            if step_result.status == "failed":
                current_attempt = (step_result.output or {}).get("attempt", 1)
                if current_attempt < step.max_retries:
                    next_step = step
                    break
                state.status = WorkflowState.derive_status(state.steps)
                try:
                    await save_with_version(persistence, state, state.version)
                except StaleStateError:
                    continue
                return

        if next_step is None:
            state.status = WorkflowState.derive_status(state.steps)
            try:
                await save_with_version(persistence, state, state.version)
            except StaleStateError:
                continue
            return

        if next_step.type == "human-approval":
            state.steps[next_step.output_key] = StepResult(
                step_name=next_step.name,
                status="awaiting_approval",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            state.status = WorkflowState.derive_status(state.steps)
            try:
                await save_with_version(persistence, state, state.version)
            except StaleStateError:
                continue
            return

        if next_step.type == "agent" and dispatcher:
            existing = state.steps.get(next_step.output_key)
            if existing and existing.status == "dispatched":
                return

            current_attempt = 1
            if existing and existing.status == "failed":
                current_attempt = (existing.output or {}).get("attempt", 0) + 1

            try:
                from agents.workflow.interpolation import interpolate
                prompt = interpolate(next_step.prompt or "", state)
            except ValueError:
                prompt = next_step.prompt or ""

            await dispatcher.dispatch_async(
                next_step, prompt, workflow_id,
                persistence=persistence, attempt=current_attempt,
            )
        return

    logger.warning("advance_workflow exhausted %d retries for %s", max_retries, workflow_id)


async def save_with_version(
    persistence: WorkflowPersistence,
    state: WorkflowState,
    expected_version: int,
) -> None:
    """Save workflow state with optimistic locking.

    Increments version on save. Raises StaleStateError if
    the current version doesn't match expected.

    Args:
        persistence: The persistence backend.
        state: The workflow state to save.
        expected_version: The version we expect to be current.

    Raises:
        StaleStateError: If another replica already advanced this workflow.
    """
    state.updated_at = datetime.now(timezone.utc).isoformat()
    if hasattr(persistence, "save_cas"):
        success = await persistence.save_cas(state, expected_version)
        if not success:
            raise StaleStateError(
                f"Workflow {state.workflow_id} version mismatch: "
                f"expected {expected_version}"
            )
    else:
        current = await persistence.load(state.workflow_id)
        if current and current.version != expected_version:
            raise StaleStateError(
                f"Workflow {state.workflow_id} version mismatch: "
                f"expected {expected_version}, got {current.version}"
            )
        state.version = expected_version + 1
        await persistence.save(state)


class RecoveryPoller:
    """Background poller that detects orphaned dispatched steps.

    Runs on every runner replica. Checks for steps stuck in
    "dispatched" status past their timeout and marks them failed.

    Attributes:
        persistence: The persistence backend.
        poll_interval: Seconds between poll cycles.
        step_timeout: Seconds before a dispatched step is considered orphaned.
    """

    def __init__(
        self,
        persistence: WorkflowPersistence,
        poll_interval: int = RECOVERY_POLL_INTERVAL,
        step_timeout: int = 600,
        spawner: Any = None,
        client_factory: Any = None,
    ) -> None:
        """Initialize the recovery poller.

        Args:
            persistence: The persistence backend.
            poll_interval: Seconds between poll cycles.
            step_timeout: Seconds before orphan detection.
            spawner: Spawner for pod cleanup.
            client_factory: Factory for creating RemoteAgentClient from endpoint.
        """
        self._persistence = persistence
        self._poll_interval = poll_interval
        self._step_timeout = step_timeout
        self._spawner = spawner
        self._client_factory = client_factory
        self._running = False

    async def start(self) -> None:
        """Start the recovery polling loop."""
        self._running = True
        logger.info("Recovery poller started (interval=%ds)", self._poll_interval)
        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                logger.warning("Recovery poll error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Stop the recovery polling loop."""
        self._running = False
        logger.info("Recovery poller stopped")

    async def _poll_once(self) -> None:
        """Check for orphaned dispatched steps, attempting result recovery."""
        workflows = await self._persistence.list_active()
        now = datetime.now(timezone.utc)
        for wf in workflows:
            if wf.status != "running":
                continue
            for key, step_result in wf.steps.items():
                if step_result.status != "dispatched":
                    continue
                if not step_result.started_at:
                    continue
                started = datetime.fromisoformat(step_result.started_at)
                elapsed = (now - started).total_seconds()
                if elapsed <= self._step_timeout:
                    continue

                output = step_result.output or {}
                run_id = output.get("run_id")
                endpoint = output.get("endpoint")
                spawned_name = output.get("spawned_name")
                attempt = output.get("attempt", 1)

                recovered = False
                if self._client_factory and endpoint:
                    try:
                        client = self._client_factory(endpoint)
                        if run_id:
                            run_state = await client.poll_run(run_id)
                            from agents.models import RunStatus
                            if run_state.status == RunStatus.COMPLETED and run_state.result:
                                payload = StepResultPayload(
                                    status="completed",
                                    output=run_state.result.output,
                                    completed_at=now.isoformat(),
                                    attempt=attempt,
                                )
                                await ingest_step_result(self._persistence, wf.workflow_id, key, payload)
                                await advance_workflow(self._persistence, None, wf.workflow_id)
                                recovered = True
                            elif run_state.status == RunStatus.FAILED:
                                error = run_state.result.error if run_state.result else "Unknown error"
                                payload = StepResultPayload(
                                    status="failed", error=error,
                                    completed_at=now.isoformat(), attempt=attempt,
                                )
                                await ingest_step_result(self._persistence, wf.workflow_id, key, payload)
                                await advance_workflow(self._persistence, None, wf.workflow_id)
                                recovered = True
                        else:
                            is_reachable = await client.healthz()
                            if not is_reachable:
                                payload = StepResultPayload(
                                    status="failed",
                                    error="Dispatch interrupted — pod never spawned or unreachable",
                                    completed_at=now.isoformat(),
                                    attempt=attempt,
                                )
                                await ingest_step_result(self._persistence, wf.workflow_id, key, payload)
                                await advance_workflow(self._persistence, None, wf.workflow_id)
                                recovered = True
                    except IngestError:
                        recovered = True
                    except Exception as exc:
                        logger.warning("Recovery poll failed for step '%s': %s", step_result.step_name, exc)

                if not recovered:
                    logger.warning(
                        "Orphaned step '%s' in workflow '%s' (dispatched %ds ago)",
                        step_result.step_name, wf.workflow_id, int(elapsed),
                    )
                    step_result.status = "failed"
                    step_result.error = f"Step timed out after {int(elapsed)}s (orphaned)"
                    step_result.completed_at = now.isoformat()
                    wf.status = WorkflowState.derive_status(wf.steps)
                    try:
                        await save_with_version(self._persistence, wf, wf.version)
                    except StaleStateError:
                        logger.info("Another replica already handled workflow %s", wf.workflow_id)
                        continue

                if self._spawner and spawned_name:
                    try:
                        await self._spawner.destroy(spawned_name)
                        logger.info("Destroyed pod '%s'", spawned_name)
                    except Exception as destroy_exc:
                        logger.warning("Failed to destroy pod '%s': %s", spawned_name, destroy_exc)
