"""Alertmanager webhook alert trigger.

Provides a REST endpoint that accepts Prometheus Alertmanager v4 webhook
payloads and maps them to workflow executions. Alert labels drive workflow
selection and input parameters. Includes in-memory dedup tracking to
prevent duplicate workflow starts within a configurable window.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.definition_store import DefinitionStore
from cloud_agents.workflow.temporal_metrics import ls_alert_triggers_total
from cloud_agents.workflow.temporal_models import ProviderConfig, WorkflowInput
from cloud_agents.workflow.temporal_worker import DEFAULT_TASK_QUEUE
from cloud_agents.workflow.temporal_workflow import AgentWorkflow

if TYPE_CHECKING:
    from cloud_agents.workflow.content_policy import ContentPolicy

logger = logging.getLogger(__name__)

# Maximum characters for alert labels/annotations embedded into prompts.
# Prevents prompt injection via excessively large alert metadata.
_MAX_ALERT_FIELD_CHARS: int = 2000


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AlertmanagerAlert(BaseModel):
    """A single alert from an Alertmanager webhook payload.

    Attributes:
        status: Alert status, either "firing" or "resolved".
        labels: Key-value labels attached to the alert.
        annotations: Key-value annotations (summary, description, etc.).
        startsAt: ISO 8601 timestamp when the alert started firing.
        endsAt: ISO 8601 timestamp when the alert resolved (or zero value).
        generatorURL: URL to the Prometheus graph for this alert.
        fingerprint: Unique identifier for the alert instance.
    """

    model_config = ConfigDict(extra="allow")

    status: str
    labels: dict[str, str]
    annotations: dict[str, str]
    startsAt: str
    endsAt: str
    generatorURL: str
    fingerprint: str


class AlertmanagerPayload(BaseModel):
    """Alertmanager v4 webhook payload.

    Attributes:
        version: Payload format version (typically "4").
        groupKey: Key identifying the alert group.
        status: Overall group status ("firing" or "resolved").
        receiver: Name of the receiver that matched.
        alerts: List of individual alerts in the group.
        groupLabels: Labels used for grouping.
        commonLabels: Labels common to all alerts in the group.
        commonAnnotations: Annotations common to all alerts.
        externalURL: URL of the Alertmanager instance.
    """

    model_config = ConfigDict(extra="allow")

    version: str
    groupKey: str
    status: str
    receiver: str
    alerts: list[AlertmanagerAlert]
    groupLabels: dict[str, str]
    commonLabels: dict[str, str]
    commonAnnotations: dict[str, str]
    externalURL: str


class AlertTriggerConfig(BaseModel):
    """Configuration for alert-to-workflow mapping.

    Attributes:
        workflow_name_label: Alert label that specifies the target workflow name.
        fire_on_resolved: Whether to trigger workflows for resolved alerts.
        default_workflow: Fallback workflow name when the label is missing.
        dedup_window_seconds: Seconds to suppress duplicate alerts by fingerprint.
    """

    workflow_name_label: str = "cloud_agents_workflow"
    fire_on_resolved: bool = False
    default_workflow: str | None = None
    dedup_window_seconds: int = 300


# ---------------------------------------------------------------------------
# Alert-to-workflow mapping
# ---------------------------------------------------------------------------


def should_process_alert(alert: AlertmanagerAlert, config: AlertTriggerConfig) -> bool:
    """Determine whether an alert should trigger a workflow.

    Parameters:
        alert: The individual alert to evaluate.
        config: Alert trigger configuration.

    Returns:
        True if the alert should be processed, False otherwise.
    """
    if alert.status == "resolved" and not config.fire_on_resolved:
        return False
    return True


def _truncate(text: str, max_chars: int = _MAX_ALERT_FIELD_CHARS) -> str:
    """Truncate text to max_chars, appending an indicator if trimmed.

    Parameters:
        text: The text to truncate.
        max_chars: Maximum allowed length.

    Returns:
        The original text if within limits, or truncated with '...[truncated]'.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def map_alert_to_workflow_input(
    alert: AlertmanagerAlert,
    config: AlertTriggerConfig,
) -> tuple[str, str]:
    """Map an alert to a workflow name and input prompt.

    Alert labels and annotations are treated as structured data and truncated
    to ``_MAX_ALERT_FIELD_CHARS`` to bound prompt size.

    Parameters:
        alert: The alert to map.
        config: Alert trigger configuration.

    Returns:
        Tuple of (workflow_name, input_prompt).

    Raises:
        ValueError: When no workflow can be determined from the alert.
    """
    workflow_name = alert.labels.get(config.workflow_name_label)
    if not workflow_name:
        workflow_name = config.default_workflow
    if not workflow_name:
        raise ValueError(
            f"No workflow name found in alert label '{config.workflow_name_label}' "
            f"and no default_workflow configured"
        )

    alertname = alert.labels.get("alertname", "unknown")
    severity = alert.labels.get("severity", "unknown")
    description = alert.annotations.get("description", alert.annotations.get("summary", ""))

    # Truncate user-controlled fields to bound prompt size and limit
    # prompt injection surface. Labels/annotations are structured input.
    truncated_description = _truncate(description)
    truncated_labels = _truncate(json.dumps(alert.labels))

    input_prompt = (
        f"Alert: {alertname}, Severity: {severity}, "
        f"Description: {truncated_description}, "
        f"Labels: {truncated_labels}"
    )

    return workflow_name, input_prompt


# ---------------------------------------------------------------------------
# Dedup tracker
# ---------------------------------------------------------------------------


class AlertDedupTracker:
    """In-memory dedup tracker for alert fingerprints.

    Prevents duplicate workflow triggers for the same alert within a
    configurable time window. Prunes expired entries to keep memory bounded.

    Attributes:
        window_seconds: Duration in seconds to suppress duplicate fingerprints.
    """

    def __init__(self, window_seconds: int = 300) -> None:
        """Initialize the dedup tracker.

        Parameters:
            window_seconds: Dedup window duration in seconds.
        """
        self.window_seconds = window_seconds
        self._last_fired: dict[str, float] = {}

    def should_fire(self, fingerprint: str) -> bool:
        """Check if a workflow should be triggered for this fingerprint.

        Also performs amortized pruning of expired entries.

        Parameters:
            fingerprint: The alert fingerprint to check.

        Returns:
            True if the alert should trigger a workflow, False if deduped.
        """
        now = time.monotonic()
        self._prune(now)

        last = self._last_fired.get(fingerprint)
        if last is not None and (now - last) < self.window_seconds:
            return False

        self._last_fired[fingerprint] = now
        return True

    def _prune(self, now: float) -> None:
        """Remove entries older than 2x the dedup window.

        Parameters:
            now: Current monotonic time.
        """
        cutoff = now - (2 * self.window_seconds)
        expired = [fp for fp, ts in self._last_fired.items() if ts < cutoff]
        for fp in expired:
            del self._last_fired[fp]


# ---------------------------------------------------------------------------
# Webhook router
# ---------------------------------------------------------------------------


def build_alert_router(
    temporal_client: Any,
    definition_store: DefinitionStore,
    config: AlertTriggerConfig | None = None,
    auth_dependency: Optional[Any] = None,
    authorizer: Optional[Any] = None,
    content_policy: Optional["ContentPolicy"] = None,
) -> APIRouter:
    """Build FastAPI router for Alertmanager webhook endpoint.

    Parameters:
        temporal_client: Connected Temporal client instance.
        definition_store: Store for workflow definition lookup.
        config: Alert trigger configuration. Defaults to AlertTriggerConfig().
        auth_dependency: Optional FastAPI auth dependency.
        authorizer: Optional WorkflowAuthorizer for fine-grained access
            control. Defaults to NoopAuthorizer (permit all).
        content_policy: Optional content policy for definition validation.
            When provided, stored definitions are re-validated before execution.

    Returns:
        APIRouter with the alertmanager webhook endpoint.
    """
    from cloud_agents.workflow.authorization import (
        CallerIdentity,
        NoopAuthorizer,
        WorkflowAction,
        WorkflowResource,
    )

    trigger_config = config or AlertTriggerConfig()
    dedup_tracker = AlertDedupTracker(window_seconds=trigger_config.dedup_window_seconds)
    authz = authorizer or NoopAuthorizer()

    alert_namespace = os.environ.get("ALERT_TRIGGER_NAMESPACE", "system")

    dependencies = [Depends(auth_dependency)] if auth_dependency else []
    router = APIRouter(
        prefix="/v1/webhooks",
        tags=["webhooks"],
        dependencies=dependencies,
    )

    @router.post("/alertmanager")
    async def receive_alertmanager(
        payload: AlertmanagerPayload,
    ) -> dict[str, Any]:
        """Receive an Alertmanager webhook and start workflows.

        For each firing alert, maps alert labels to a workflow definition
        and starts a workflow execution via Temporal. Dedup tracking
        prevents duplicate workflows within the configured window.

        Parameters:
            payload: The Alertmanager v4 webhook payload.

        Returns:
            Summary dict with workflows_started, alerts_skipped, and errors.
        """
        started = 0
        skipped = 0
        errors = 0

        # Build a synthetic caller identity for authorization checks.
        alert_caller = CallerIdentity(
            username="alertmanager",
            groups=[f"{alert_namespace}:alertmanager"],
            auth_mode="webhook",
        )

        for alert in payload.alerts:
            alertname = alert.labels.get("alertname", "unknown")

            # Check if alert should be processed
            if not should_process_alert(alert, trigger_config):
                skipped += 1
                ls_alert_triggers_total.labels(
                    workflow_name="unknown", status="skipped_resolved"
                ).inc()
                logger.debug(
                    "Skipping resolved alert '%s' (fingerprint=%s)",
                    alertname,
                    alert.fingerprint,
                )
                continue

            # Dedup check
            if not dedup_tracker.should_fire(alert.fingerprint):
                skipped += 1
                ls_alert_triggers_total.labels(
                    workflow_name="unknown", status="skipped_dedup"
                ).inc()
                logger.debug(
                    "Dedup: skipping alert '%s' (fingerprint=%s)",
                    alertname,
                    alert.fingerprint,
                )
                continue

            # Map alert to workflow
            try:
                workflow_name, input_prompt = map_alert_to_workflow_input(
                    alert, trigger_config
                )
            except ValueError as exc:
                errors += 1
                ls_alert_triggers_total.labels(
                    workflow_name="unknown", status="error"
                ).inc()
                logger.warning("Alert mapping failed for '%s': %s", alertname, exc)
                emit_audit(
                    event_type="alert_validation_failed",
                    workflow_id="",
                    details={
                        "alertname": alertname,
                        "fingerprint": alert.fingerprint,
                        "error": str(exc),
                    },
                )
                continue

            # --- Authorization check ---
            decision = await authz.authorize(
                alert_caller,
                WorkflowAction.TRIGGER,
                WorkflowResource(workflow_name=workflow_name),
            )
            if not decision.allowed:
                errors += 1
                ls_alert_triggers_total.labels(
                    workflow_name=workflow_name, status="error"
                ).inc()
                logger.warning(
                    "Authorization denied for alert '%s' -> workflow '%s': %s",
                    alertname,
                    workflow_name,
                    decision.reason,
                )
                emit_audit(
                    event_type="alert_authorization_denied",
                    workflow_id="",
                    details={
                        "alertname": alertname,
                        "workflow_name": workflow_name,
                        "reason": decision.reason,
                    },
                )
                continue

            # Look up workflow definition
            stored = await definition_store.get(workflow_name)
            if not stored:
                errors += 1
                ls_alert_triggers_total.labels(
                    workflow_name=workflow_name, status="error"
                ).inc()
                logger.warning(
                    "Workflow definition '%s' not found for alert '%s'",
                    workflow_name,
                    alertname,
                )
                emit_audit(
                    event_type="alert_validation_failed",
                    workflow_id="",
                    details={
                        "alertname": alertname,
                        "workflow_name": workflow_name,
                        "error": f"Definition '{workflow_name}' not found",
                    },
                )
                continue

            # Build workflow input
            definition = stored.definition.model_dump()

            # --- Content policy re-validation ---
            if content_policy is not None:
                from cloud_agents.workflow.temporal_validation import validate_definition

                validation_errors = validate_definition(
                    definition, content_policy=content_policy
                )
                if validation_errors:
                    errors += 1
                    ls_alert_triggers_total.labels(
                        workflow_name=workflow_name, status="error"
                    ).inc()
                    logger.warning(
                        "Content policy violation for workflow '%s' "
                        "triggered by alert '%s': %s",
                        workflow_name,
                        alertname,
                        validation_errors,
                    )
                    emit_audit(
                        event_type="content_policy_violation",
                        workflow_id="",
                        details={
                            "alertname": alertname,
                            "workflow_name": workflow_name,
                            "violations": validation_errors,
                        },
                    )
                    continue

            if stored.definition.provider:
                provider = ProviderConfig(**stored.definition.provider.model_dump())
            else:
                errors += 1
                ls_alert_triggers_total.labels(
                    workflow_name=workflow_name, status="error"
                ).inc()
                logger.warning(
                    "Workflow '%s' has no provider configured for alert '%s'",
                    workflow_name,
                    alertname,
                )
                emit_audit(
                    event_type="alert_validation_failed",
                    workflow_id="",
                    details={
                        "alertname": alertname,
                        "workflow_name": workflow_name,
                        "error": f"Definition '{workflow_name}' has no provider",
                    },
                )
                continue

            workflow_id = f"alert-{alert.fingerprint}-{uuid.uuid4().hex[:8]}"

            from cloud_agents.workflow.authorization import WorkflowAuthzContext

            authz_ctx = WorkflowAuthzContext(
                owner_username="alertmanager",
                owner_groups=[f"{alert_namespace}:alertmanager"],
                workflow_name=workflow_name,
                namespace=alert_namespace,
            )

            workflow_input = WorkflowInput(
                definition=definition,
                input_prompt=input_prompt,
                workflow_id=workflow_id,
                provider=provider,
                authz_context=authz_ctx,
            )

            # Start workflow
            try:
                await temporal_client.start_workflow(
                    AgentWorkflow.run,
                    workflow_input,
                    id=workflow_id,
                    task_queue=DEFAULT_TASK_QUEUE,
                )
                started += 1
                ls_alert_triggers_total.labels(
                    workflow_name=workflow_name, status="started"
                ).inc()
                logger.info(
                    "Started workflow '%s' for alert '%s' (fingerprint=%s)",
                    workflow_id,
                    alertname,
                    alert.fingerprint,
                )
                emit_audit(
                    event_type="alert_triggered",
                    workflow_id=workflow_id,
                    details={
                        "alertname": alertname,
                        "workflow_name": workflow_name,
                        "fingerprint": alert.fingerprint,
                        "alert_status": alert.status,
                    },
                )
            except Exception as exc:
                errors += 1
                ls_alert_triggers_total.labels(
                    workflow_name=workflow_name, status="error"
                ).inc()
                logger.error(
                    "Failed to start workflow for alert '%s': %s",
                    alertname,
                    exc,
                )

        return {
            "status": "ok",
            "workflows_started": started,
            "alerts_skipped": skipped,
            "errors": errors,
        }

    return router
