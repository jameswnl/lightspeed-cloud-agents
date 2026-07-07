"""Unit tests for Alertmanager webhook alert trigger (TDD).

Tests cover: payload parsing, alert-to-workflow mapping, dedup tracking,
webhook endpoint behavior, config wiring, Prometheus metrics,
authorization, and content policy enforcement.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ALERT: dict[str, Any] = {
    "status": "firing",
    "labels": {
        "alertname": "HighCPU",
        "severity": "critical",
        "cloud_agents_workflow": "diagnose-cpu",
    },
    "annotations": {
        "summary": "CPU usage above 90%",
        "description": "Instance i-abc has high CPU",
    },
    "startsAt": "2025-01-15T10:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "generatorURL": "http://prometheus:9090/graph?g0.expr=up",
    "fingerprint": "abc123",
}

SAMPLE_PAYLOAD: dict[str, Any] = {
    "version": "4",
    "groupKey": "{}:{alertname='HighCPU'}",
    "status": "firing",
    "receiver": "cloud-agents",
    "alerts": [SAMPLE_ALERT],
    "groupLabels": {"alertname": "HighCPU"},
    "commonLabels": {"alertname": "HighCPU", "severity": "critical"},
    "commonAnnotations": {"summary": "CPU usage above 90%"},
    "externalURL": "http://alertmanager:9093",
}


# ===========================================================================
# 1. Pydantic model parsing
# ===========================================================================


class TestAlertmanagerModels:
    """Tests for Alertmanager payload Pydantic models."""

    def test_valid_payload_parses(self) -> None:
        """Valid Alertmanager v4 payload parses correctly."""
        from cloud_agents.workflow.alert_trigger import AlertmanagerPayload

        payload = AlertmanagerPayload.model_validate(SAMPLE_PAYLOAD)
        assert payload.version == "4"
        assert len(payload.alerts) == 1
        assert payload.alerts[0].status == "firing"
        assert payload.alerts[0].labels["alertname"] == "HighCPU"

    def test_payload_with_missing_optional_fields(self) -> None:
        """Payload with missing optional fields parses with defaults."""
        from cloud_agents.workflow.alert_trigger import AlertmanagerPayload

        minimal = {
            "version": "4",
            "groupKey": "test",
            "status": "firing",
            "receiver": "test",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "Test"},
                    "annotations": {},
                    "startsAt": "2025-01-01T00:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "",
                    "fingerprint": "fp1",
                },
            ],
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "",
        }
        payload = AlertmanagerPayload.model_validate(minimal)
        assert payload.status == "firing"

    def test_payload_accepts_extra_fields(self) -> None:
        """Payload with unknown extra fields is accepted (forward compat)."""
        from cloud_agents.workflow.alert_trigger import AlertmanagerPayload

        extended = {**SAMPLE_PAYLOAD, "truncatedAlerts": 0, "futureField": "value"}
        payload = AlertmanagerPayload.model_validate(extended)
        assert payload.version == "4"

    def test_alert_trigger_config_defaults(self) -> None:
        """AlertTriggerConfig has sensible defaults."""
        from cloud_agents.workflow.alert_trigger import AlertTriggerConfig

        config = AlertTriggerConfig()
        assert config.workflow_name_label == "cloud_agents_workflow"
        assert config.fire_on_resolved is False
        assert config.default_workflow is None
        assert config.dedup_window_seconds == 300


# ===========================================================================
# 2. Alert-to-workflow mapping
# ===========================================================================


class TestAlertToWorkflowMapping:
    """Tests for alert-to-workflow mapping logic."""

    def test_alert_with_workflow_label_maps_to_workflow(self) -> None:
        """Alert with cloud_agents_workflow label maps to that workflow name."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            map_alert_to_workflow_input,
        )

        alert = AlertmanagerAlert.model_validate(SAMPLE_ALERT)
        config = AlertTriggerConfig()
        wf_name, input_prompt = map_alert_to_workflow_input(alert, config)
        assert wf_name == "diagnose-cpu"
        assert "HighCPU" in input_prompt

    def test_alert_without_label_uses_default_workflow(self) -> None:
        """Alert without workflow label falls back to default_workflow."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            map_alert_to_workflow_input,
        )

        alert_data = {**SAMPLE_ALERT, "labels": {"alertname": "DiskFull", "severity": "warning"}}
        alert = AlertmanagerAlert.model_validate(alert_data)
        config = AlertTriggerConfig(default_workflow="generic-diag")
        wf_name, _ = map_alert_to_workflow_input(alert, config)
        assert wf_name == "generic-diag"

    def test_alert_without_label_and_no_default_raises(self) -> None:
        """Alert without workflow label and no default raises ValueError."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            map_alert_to_workflow_input,
        )

        alert_data = {**SAMPLE_ALERT, "labels": {"alertname": "NoWorkflow"}}
        alert = AlertmanagerAlert.model_validate(alert_data)
        config = AlertTriggerConfig()
        with pytest.raises(ValueError, match="No workflow"):
            map_alert_to_workflow_input(alert, config)

    def test_input_prompt_includes_alert_details(self) -> None:
        """Input prompt includes alertname, severity, and description."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            map_alert_to_workflow_input,
        )

        alert = AlertmanagerAlert.model_validate(SAMPLE_ALERT)
        config = AlertTriggerConfig()
        _, input_prompt = map_alert_to_workflow_input(alert, config)
        assert "HighCPU" in input_prompt
        assert "critical" in input_prompt
        assert "Instance i-abc has high CPU" in input_prompt

    def test_only_firing_alerts_trigger_by_default(self) -> None:
        """Only firing alerts trigger workflows by default."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            should_process_alert,
        )

        config = AlertTriggerConfig()

        firing = AlertmanagerAlert.model_validate(SAMPLE_ALERT)
        assert should_process_alert(firing, config) is True

        resolved_data = {**SAMPLE_ALERT, "status": "resolved"}
        resolved = AlertmanagerAlert.model_validate(resolved_data)
        assert should_process_alert(resolved, config) is False

    def test_resolved_alerts_trigger_when_configured(self) -> None:
        """Resolved alerts trigger when fire_on_resolved=True."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            should_process_alert,
        )

        config = AlertTriggerConfig(fire_on_resolved=True)
        resolved_data = {**SAMPLE_ALERT, "status": "resolved"}
        resolved = AlertmanagerAlert.model_validate(resolved_data)
        assert should_process_alert(resolved, config) is True

    def test_long_labels_truncated_in_prompt(self) -> None:
        """Labels exceeding max chars are truncated in the input prompt."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            _MAX_ALERT_FIELD_CHARS,
            map_alert_to_workflow_input,
        )

        long_description = "x" * (_MAX_ALERT_FIELD_CHARS + 500)
        alert_data = {
            **SAMPLE_ALERT,
            "annotations": {"description": long_description},
        }
        alert = AlertmanagerAlert.model_validate(alert_data)
        config = AlertTriggerConfig()
        _, input_prompt = map_alert_to_workflow_input(alert, config)
        # Full description should NOT appear — it should be truncated.
        assert long_description not in input_prompt
        assert "...[truncated]" in input_prompt

    def test_short_labels_not_truncated(self) -> None:
        """Labels within limit are not truncated."""
        from cloud_agents.workflow.alert_trigger import (
            AlertmanagerAlert,
            AlertTriggerConfig,
            map_alert_to_workflow_input,
        )

        alert = AlertmanagerAlert.model_validate(SAMPLE_ALERT)
        config = AlertTriggerConfig()
        _, input_prompt = map_alert_to_workflow_input(alert, config)
        assert "...[truncated]" not in input_prompt


# ===========================================================================
# 3. Dedup tracker
# ===========================================================================


class TestAlertDedupTracker:
    """Tests for in-memory alert dedup tracker."""

    def test_first_occurrence_returns_true(self) -> None:
        """First occurrence of a fingerprint returns True."""
        from cloud_agents.workflow.alert_trigger import AlertDedupTracker

        tracker = AlertDedupTracker(window_seconds=300)
        assert tracker.should_fire("fp-new") is True

    def test_same_fingerprint_within_window_returns_false(self) -> None:
        """Same fingerprint within dedup window returns False."""
        from cloud_agents.workflow.alert_trigger import AlertDedupTracker

        tracker = AlertDedupTracker(window_seconds=300)
        tracker.should_fire("fp-dup")
        assert tracker.should_fire("fp-dup") is False

    def test_same_fingerprint_after_window_returns_true(self) -> None:
        """Same fingerprint after dedup window expires returns True."""
        from cloud_agents.workflow.alert_trigger import AlertDedupTracker

        tracker = AlertDedupTracker(window_seconds=0)
        tracker.should_fire("fp-expired")
        # window_seconds=0 means the entry is already expired
        assert tracker.should_fire("fp-expired") is True

    def test_different_fingerprints_independent(self) -> None:
        """Different fingerprints are tracked independently."""
        from cloud_agents.workflow.alert_trigger import AlertDedupTracker

        tracker = AlertDedupTracker(window_seconds=300)
        assert tracker.should_fire("fp-a") is True
        assert tracker.should_fire("fp-b") is True
        assert tracker.should_fire("fp-a") is False
        assert tracker.should_fire("fp-b") is False

    def test_pruning_removes_old_entries(self) -> None:
        """Old entries are pruned to keep memory bounded."""
        from cloud_agents.workflow.alert_trigger import AlertDedupTracker

        tracker = AlertDedupTracker(window_seconds=0)
        tracker.should_fire("fp-old-1")
        tracker.should_fire("fp-old-2")
        tracker.should_fire("fp-new")
        assert tracker.should_fire("fp-old-1") is True


# ===========================================================================
# 4. Webhook endpoint
# ===========================================================================


def _build_alert_app(
    mock_temporal: Any,
    definition_store: Any = None,
    alert_config: Any = None,
    auth_dependency: Any = None,
    authorizer: Any = None,
    content_policy: Any = None,
) -> FastAPI:
    """Build a FastAPI app with the alert webhook router for testing."""
    from cloud_agents.workflow.alert_trigger import (
        AlertTriggerConfig,
        build_alert_router,
    )
    from cloud_agents.workflow.definition_store import DefinitionStore

    store = definition_store or DefinitionStore()
    config = alert_config or AlertTriggerConfig()
    app = FastAPI()
    router = build_alert_router(
        temporal_client=mock_temporal,
        definition_store=store,
        config=config,
        auth_dependency=auth_dependency,
        authorizer=authorizer,
        content_policy=content_policy,
    )
    app.include_router(router)
    return app


def _make_stored_definition(name: str = "diagnose-cpu") -> Any:
    """Create a mock StoredDefinition for testing."""
    from unittest.mock import MagicMock

    from cloud_agents.workflow.definition import WorkflowDefinition

    defn = WorkflowDefinition.model_validate(
        {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": name},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "output_key": "r1",
                        "prompt": "diagnose",
                    }
                ]
            },
            "provider": {
                "name": "openai",
                "model": "gpt-4",
                "credentials_secret": "test-key",
            },
        }
    )
    stored = MagicMock()
    stored.name = name
    stored.version = 1
    stored.definition = defn
    return stored


class TestAlertWebhookEndpoint:
    """Tests for POST /v1/webhooks/alertmanager endpoint."""

    def test_valid_payload_starts_workflow(self, mocker: MockerFixture) -> None:
        """Valid Alertmanager payload starts workflow via Temporal client."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["workflows_started"] == 1
        assert body["alerts_skipped"] == 0
        mock_temporal.start_workflow.assert_called_once()

    def test_resolved_alerts_skipped_by_default(self, mocker: MockerFixture) -> None:
        """Resolved alerts are skipped by default."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")
        resolved_payload = {**SAMPLE_PAYLOAD, "alerts": [{**SAMPLE_ALERT, "status": "resolved"}]}

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=resolved_payload)
        assert response.status_code == 200
        assert response.json()["alerts_skipped"] == 1
        assert response.json()["workflows_started"] == 0
        mock_temporal.start_workflow.assert_not_called()

    def test_unknown_workflow_returns_partial_success(self, mocker: MockerFixture) -> None:
        """Unknown workflow name results in error count in response."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=None)
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        body = response.json()
        assert body["workflows_started"] == 0
        assert body["errors"] >= 1

    def test_dedup_prevents_duplicate_starts(self, mocker: MockerFixture) -> None:
        """Dedup prevents duplicate workflow starts for same fingerprint."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response1 = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response1.json()["workflows_started"] == 1
        response2 = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response2.json()["workflows_started"] == 0
        assert response2.json()["alerts_skipped"] >= 1

    def test_audit_event_emitted(self, mocker: MockerFixture) -> None:
        """Audit event emitted for each triggered workflow."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mock_emit = mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)

        triggered_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "alert_triggered"
        ]
        assert len(triggered_calls) == 1
        details = triggered_calls[0][1]["details"]
        assert details["alertname"] == "HighCPU"
        assert details["workflow_name"] == "diagnose-cpu"

    def test_multiple_alerts_start_multiple_workflows(self, mocker: MockerFixture) -> None:
        """Multiple alerts in one payload start multiple workflows."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        second_alert = {
            **SAMPLE_ALERT,
            "labels": {"alertname": "DiskFull", "severity": "warning", "cloud_agents_workflow": "diagnose-cpu"},
            "fingerprint": "def456",
        }
        multi_payload = {**SAMPLE_PAYLOAD, "alerts": [SAMPLE_ALERT, second_alert]}

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=multi_payload)
        assert response.status_code == 200
        assert response.json()["workflows_started"] == 2
        assert mock_temporal.start_workflow.call_count == 2

    def test_auth_enforced_when_configured(self, mocker: MockerFixture) -> None:
        """Auth required when auth_dependency is set."""
        def reject_unauthenticated():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

        mock_temporal = mocker.MagicMock()
        app = _build_alert_app(mock_temporal, auth_dependency=reject_unauthenticated)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 401


# ===========================================================================
# 5. Authorization
# ===========================================================================


class TestAlertTriggerAuthorization:
    """Tests for authorization enforcement in alert trigger."""

    def test_authorizer_called_before_workflow_start(self, mocker: MockerFixture) -> None:
        """Authorizer is called with TRIGGER action before starting workflow."""
        from cloud_agents.workflow.authorization import AuthzDecision, WorkflowAction

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        mock_authorizer = mocker.AsyncMock()
        mock_authorizer.authorize = mocker.AsyncMock(
            return_value=AuthzDecision(allowed=True, reason="ok")
        )

        app = _build_alert_app(mock_temporal, definition_store=store, authorizer=mock_authorizer)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["workflows_started"] == 1

        mock_authorizer.authorize.assert_called_once()
        call_args = mock_authorizer.authorize.call_args
        assert call_args[0][1] == WorkflowAction.TRIGGER

    def test_authorizer_denies_blocks_workflow(self, mocker: MockerFixture) -> None:
        """Denied authorization prevents workflow start."""
        from cloud_agents.workflow.authorization import AuthzDecision

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        mock_authorizer = mocker.AsyncMock()
        mock_authorizer.authorize = mocker.AsyncMock(
            return_value=AuthzDecision(allowed=False, reason="not allowed")
        )

        app = _build_alert_app(mock_temporal, definition_store=store, authorizer=mock_authorizer)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        body = response.json()
        assert body["workflows_started"] == 0
        assert body["errors"] == 1
        mock_temporal.start_workflow.assert_not_called()

    def test_noop_authorizer_used_by_default(self, mocker: MockerFixture) -> None:
        """NoopAuthorizer is used when no authorizer is provided."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["workflows_started"] == 1

    def test_authz_context_includes_namespace_and_groups(
        self, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WorkflowAuthzContext includes configured namespace and groups."""
        monkeypatch.setenv("ALERT_TRIGGER_NAMESPACE", "prod")

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["workflows_started"] == 1

        call_args = mock_temporal.start_workflow.call_args
        workflow_input = call_args[0][1]
        assert workflow_input.authz_context.namespace == "prod"
        assert "prod:alertmanager" in workflow_input.authz_context.owner_groups

    def test_authorization_denied_audit_event(self, mocker: MockerFixture) -> None:
        """Audit event emitted when authorization is denied."""
        from cloud_agents.workflow.authorization import AuthzDecision

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mock_emit = mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        mock_authorizer = mocker.AsyncMock()
        mock_authorizer.authorize = mocker.AsyncMock(
            return_value=AuthzDecision(allowed=False, reason="denied by policy")
        )

        app = _build_alert_app(mock_temporal, definition_store=store, authorizer=mock_authorizer)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)

        denied_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "alert_authorization_denied"
        ]
        assert len(denied_calls) == 1
        details = denied_calls[0][1]["details"]
        assert details["workflow_name"] == "diagnose-cpu"
        assert details["reason"] == "denied by policy"


# ===========================================================================
# 6. Content policy enforcement
# ===========================================================================


class TestAlertTriggerContentPolicy:
    """Tests for content policy re-validation in alert trigger."""

    def test_content_policy_blocks_violating_definition(self, mocker: MockerFixture) -> None:
        """Content policy violations prevent workflow start."""
        from cloud_agents.workflow.content_policy import ContentPolicy

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        policy = ContentPolicy(max_prompt_length=3)
        app = _build_alert_app(mock_temporal, definition_store=store, content_policy=policy)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        body = response.json()
        assert body["workflows_started"] == 0
        assert body["errors"] == 1
        mock_temporal.start_workflow.assert_not_called()

    def test_content_policy_allows_compliant_definition(self, mocker: MockerFixture) -> None:
        """Compliant definitions pass content policy check."""
        from cloud_agents.workflow.content_policy import ContentPolicy

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        policy = ContentPolicy(max_prompt_length=10000)
        app = _build_alert_app(mock_temporal, definition_store=store, content_policy=policy)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["workflows_started"] == 1

    def test_no_content_policy_skips_validation(self, mocker: MockerFixture) -> None:
        """No content policy means definitions are not re-validated."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["workflows_started"] == 1

    def test_content_policy_violation_audit_event(self, mocker: MockerFixture) -> None:
        """Audit event emitted for content policy violations."""
        from cloud_agents.workflow.content_policy import ContentPolicy

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mock_emit = mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        policy = ContentPolicy(max_prompt_length=3)
        app = _build_alert_app(mock_temporal, definition_store=store, content_policy=policy)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)

        violation_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "content_policy_violation"
        ]
        assert len(violation_calls) == 1
        details = violation_calls[0][1]["details"]
        assert details["workflow_name"] == "diagnose-cpu"
        assert details["alertname"] == "HighCPU"


# ===========================================================================
# 7. Entrypoint wiring
# ===========================================================================


class TestAlertTriggerEntrypointWiring:
    """Tests for alert trigger config wiring in entrypoint."""

    def test_alert_endpoint_not_registered_when_disabled(
        self, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Endpoint not registered when ALERT_TRIGGER_ENABLED=false."""
        monkeypatch.setenv("ALERT_TRIGGER_ENABLED", "false")
        import importlib
        import cloud_agents.workflow.temporal_entrypoint as ep_mod
        importlib.reload(ep_mod)

        app = ep_mod.build_temporal_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code in (404, 405)

    def test_alert_endpoint_registered_when_enabled(
        self, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Endpoint registered when ALERT_TRIGGER_ENABLED=true."""
        monkeypatch.setenv("ALERT_TRIGGER_ENABLED", "true")
        import importlib
        import cloud_agents.workflow.temporal_entrypoint as ep_mod
        importlib.reload(ep_mod)

        app = ep_mod.build_temporal_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        assert response.status_code not in (404, 405), (
            f"Alert endpoint should be registered but got {response.status_code}"
        )


# ===========================================================================
# 8. Prometheus metrics
# ===========================================================================


class TestAlertTriggerMetrics:
    """Tests for alert trigger Prometheus metrics."""

    def test_counter_incremented_on_trigger(self, mocker: MockerFixture) -> None:
        """Counter incremented on successful trigger."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        from cloud_agents.workflow.temporal_metrics import ls_alert_triggers_total
        before = ls_alert_triggers_total.labels(
            workflow_name="diagnose-cpu", status="started"
        )._value.get()

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)

        after = ls_alert_triggers_total.labels(
            workflow_name="diagnose-cpu", status="started"
        )._value.get()
        assert after > before

    def test_counter_incremented_on_dedup_skip(self, mocker: MockerFixture) -> None:
        """Counter incremented on dedup skip."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())
        mocker.patch("cloud_agents.workflow.alert_trigger.emit_audit")

        from cloud_agents.workflow.temporal_metrics import ls_alert_triggers_total

        app = _build_alert_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)

        before = ls_alert_triggers_total.labels(
            workflow_name="unknown", status="skipped_dedup"
        )._value.get()
        client.post("/v1/webhooks/alertmanager", json=SAMPLE_PAYLOAD)
        after = ls_alert_triggers_total.labels(
            workflow_name="unknown", status="skipped_dedup"
        )._value.get()
        assert after > before
