"""Unit tests for structured audit events."""

from __future__ import annotations

import json
import logging

import pytest

from cloud_agents.workflow.audit import AuditEvent, emit_audit


class TestAuditEvent:
    """Tests for AuditEvent model."""

    def test_audit_event_creation(self) -> None:
        """AuditEvent captures all required fields."""
        event = AuditEvent(
            event_type="workflow_started",
            workflow_id="wf-123",
            step_name=None,
            actor="user@redhat.com",
            risk_level=None,
            details={"definition": "diagnose-and-fix"},
        )
        assert event.event_type == "workflow_started"
        assert event.workflow_id == "wf-123"
        assert event.actor == "user@redhat.com"
        assert event.timestamp is not None

    def test_audit_event_with_step(self) -> None:
        """AuditEvent captures step-level details."""
        event = AuditEvent(
            event_type="step_approved",
            workflow_id="wf-456",
            step_name="approve",
            actor="sre-lead@redhat.com",
            risk_level="high",
            details={"approved": True},
        )
        assert event.step_name == "approve"
        assert event.risk_level == "high"

    def test_audit_event_serializes_to_json(self) -> None:
        """AuditEvent can be serialized to JSON."""
        event = AuditEvent(
            event_type="sandbox_spawned",
            workflow_id="wf-789",
            step_name="diagnose",
            actor=None,
            risk_level="low",
            details={"pod_name": "ca-abc123", "image": "sandbox:latest"},
        )
        data = json.loads(event.model_dump_json())
        assert data["event_type"] == "sandbox_spawned"
        assert data["details"]["pod_name"] == "ca-abc123"


class TestEmitAudit:
    """Tests for emit_audit helper."""

    def test_emit_audit_logs_json(self, caplog: pytest.LogCaptureFixture) -> None:
        """emit_audit writes audit event to the audit logger."""
        with caplog.at_level(logging.INFO, logger="cloud_agents.workflow.audit"):
            emit_audit(
                event_type="workflow_started",
                workflow_id="wf-test",
                details={"name": "diagnose"},
            )
        assert any("workflow_started" in r.message for r in caplog.records)

    def test_emit_audit_includes_workflow_id(self, caplog: pytest.LogCaptureFixture) -> None:
        """emit_audit log message includes workflow_id."""
        with caplog.at_level(logging.INFO, logger="cloud_agents.workflow.audit"):
            emit_audit(
                event_type="sandbox_destroyed",
                workflow_id="wf-xyz",
                step_name="fix",
                details={"pod_name": "ca-123"},
            )
        assert any("wf-xyz" in r.message for r in caplog.records)

    def test_tls_error_is_valid_audit_event_type(self) -> None:
        """tls_error is a valid AuditEventType."""
        event = AuditEvent(
            event_type="tls_error",
            workflow_id="wf-tls",
            step_name="diag",
            details={"error": "cert expired"},
        )
        assert event.event_type == "tls_error"


class TestCLISessionAuditEvents:
    """Tests for CLI session audit event types (T15 Phase 2, Task 4)."""

    def test_cli_session_launched_is_valid_event_type(self) -> None:
        """cli_session_launched is accepted as a valid AuditEventType."""
        event = AuditEvent(
            event_type="cli_session_launched",
            workflow_id="wf-cli-1",
            details={
                "session_id": "sess-abc",
                "agent_name": "cli-agent-abc",
                "workflow_id": "wf-cli-1",
            },
        )
        assert event.event_type == "cli_session_launched"
        assert event.details["session_id"] == "sess-abc"

    def test_cli_session_terminated_is_valid_event_type(self) -> None:
        """cli_session_terminated is accepted as a valid AuditEventType."""
        event = AuditEvent(
            event_type="cli_session_terminated",
            workflow_id="wf-cli-2",
            details={
                "session_id": "sess-def",
                "reason": "user_request",
            },
        )
        assert event.event_type == "cli_session_terminated"
        assert event.details["reason"] == "user_request"

    def test_cli_session_failed_is_valid_event_type(self) -> None:
        """cli_session_failed is accepted as a valid AuditEventType."""
        event = AuditEvent(
            event_type="cli_session_failed",
            workflow_id="wf-cli-3",
            details={
                "session_id": "sess-ghi",
                "error": "spawner returned 500",
            },
        )
        assert event.event_type == "cli_session_failed"
        assert event.details["error"] == "spawner returned 500"

    def test_emit_cli_session_launched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """emit_audit with cli_session_launched logs correctly."""
        with caplog.at_level(logging.INFO, logger="cloud_agents.workflow.audit"):
            event = emit_audit(
                event_type="cli_session_launched",
                workflow_id="wf-launch-test",
                details={"session_id": "sess-001", "agent_name": "cli-agent-001"},
            )
        assert event.event_type == "cli_session_launched"
        assert any("cli_session_launched" in r.message for r in caplog.records)
