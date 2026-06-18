"""Unit tests for cloud agent shared models."""

import pytest
from pydantic import ValidationError

from agents.models import (
    AgentRunRequest,
    AgentRunResponse,
    DiagnosticReport,
    RemediationAction,
)


class TestAgentRunRequest:
    """Tests for the AgentRunRequest model."""

    def test_valid_request(self) -> None:
        """Test creating a valid request with prompt only."""
        req = AgentRunRequest(prompt="Check all hosts")
        assert req.prompt == "Check all hosts"
        assert req.context is None

    def test_valid_request_with_context(self) -> None:
        """Test creating a valid request with context."""
        req = AgentRunRequest(
            prompt="Investigate web-02",
            context={"correlation_id": "abc-123", "trace_id": "xyz-789"},
        )
        assert req.prompt == "Investigate web-02"
        assert req.context["correlation_id"] == "abc-123"

    def test_empty_prompt_rejected(self) -> None:
        """Test that an empty prompt is rejected."""
        with pytest.raises(ValidationError):
            AgentRunRequest(prompt="")

    def test_whitespace_only_prompt_rejected(self) -> None:
        """Test that a whitespace-only prompt is rejected."""
        with pytest.raises(ValidationError):
            AgentRunRequest(prompt="   ")

    def test_json_round_trip(self) -> None:
        """Test serialization and deserialization through JSON."""
        req = AgentRunRequest(
            prompt="Diagnose cluster",
            context={"correlation_id": "test-123"},
        )
        json_str = req.model_dump_json()
        restored = AgentRunRequest.model_validate_json(json_str)
        assert restored.prompt == req.prompt
        assert restored.context == req.context


class TestRemediationAction:
    """Tests for the RemediationAction model."""

    def test_successful_action(self) -> None:
        """Test creating a successful remediation action."""
        action = RemediationAction(
            host="web-02",
            action="rollback_deploy:frontend",
            result="Rolled back frontend on web-02",
            success=True,
        )
        assert action.host == "web-02"
        assert action.success is True

    def test_failed_action(self) -> None:
        """Test creating a failed remediation action."""
        action = RemediationAction(
            host="db-01",
            action="cleanup_disk",
            result="Disk usage already acceptable",
            success=False,
        )
        assert action.success is False


class TestDiagnosticReport:
    """Tests for the DiagnosticReport model."""

    def test_healthy_report(self) -> None:
        """Test creating a report with successful remediation."""
        report = DiagnosticReport(
            summary="Fixed web-02",
            issues_found=["web-02: app crashed"],
            actions_taken=[
                RemediationAction(
                    host="web-02",
                    action="rollback_deploy:frontend",
                    result="Rolled back",
                    success=True,
                )
            ],
            cluster_healthy=True,
        )
        assert report.cluster_healthy is True
        assert len(report.actions_taken) == 1
        assert report.remaining_issues == []

    def test_report_with_remaining_issues(self) -> None:
        """Test creating a report with unresolved issues."""
        report = DiagnosticReport(
            summary="Partial fix",
            issues_found=["web-02: crashed", "db-01: disk full"],
            actions_taken=[
                RemediationAction(
                    host="web-02",
                    action="restart_service:app",
                    result="Restarted",
                    success=True,
                )
            ],
            remaining_issues=["db-01: disk still at 95%"],
            cluster_healthy=False,
        )
        assert report.cluster_healthy is False
        assert len(report.remaining_issues) == 1

    def test_json_round_trip_with_nested_actions(self) -> None:
        """Test that DiagnosticReport serializes and deserializes with nested actions."""
        report = DiagnosticReport(
            summary="Full fix",
            issues_found=["issue-1", "issue-2"],
            actions_taken=[
                RemediationAction(
                    host="web-02", action="rollback", result="ok", success=True
                ),
                RemediationAction(
                    host="db-01", action="cleanup", result="ok", success=True
                ),
            ],
            cluster_healthy=True,
        )
        json_str = report.model_dump_json()
        restored = DiagnosticReport.model_validate_json(json_str)
        assert len(restored.actions_taken) == 2
        assert restored.actions_taken[0].host == "web-02"
        assert restored.actions_taken[1].host == "db-01"


class TestAgentRunResponse:
    """Tests for the AgentRunResponse model."""

    def test_successful_response(self) -> None:
        """Test creating a successful response with DiagnosticReport output."""
        report = DiagnosticReport(
            summary="Fixed",
            issues_found=["issue-1"],
            actions_taken=[
                RemediationAction(
                    host="web-02", action="fix", result="ok", success=True
                )
            ],
            cluster_healthy=True,
        )
        resp = AgentRunResponse(
            output=report.model_dump(),
            output_type="DiagnosticReport",
            usage={"input_tokens": 100, "output_tokens": 200},
            agent_name="diagnostic-agent",
            success=True,
        )
        assert resp.success is True
        assert resp.output_type == "DiagnosticReport"
        assert resp.schema_version == "v1"
        assert resp.error is None

    def test_error_response(self) -> None:
        """Test creating an error response."""
        resp = AgentRunResponse(
            output={},
            output_type="error",
            usage={"input_tokens": 50, "output_tokens": 0},
            agent_name="diagnostic-agent",
            success=False,
            error="Agent timed out after 600 seconds",
        )
        assert resp.success is False
        assert resp.error == "Agent timed out after 600 seconds"

    def test_json_round_trip(self) -> None:
        """Test full serialization round-trip."""
        report = DiagnosticReport(
            summary="Test",
            issues_found=["a"],
            actions_taken=[],
            cluster_healthy=True,
        )
        resp = AgentRunResponse(
            output=report.model_dump(),
            output_type="DiagnosticReport",
            schema_version="v1",
            usage={"input_tokens": 10, "output_tokens": 20},
            agent_name="test-agent",
            success=True,
        )
        json_str = resp.model_dump_json()
        restored = AgentRunResponse.model_validate_json(json_str)
        assert restored.agent_name == "test-agent"
        assert restored.output_type == "DiagnosticReport"
        assert restored.output["summary"] == "Test"

    def test_output_can_reconstruct_diagnostic_report(self) -> None:
        """Test that the output dict can be used to reconstruct a DiagnosticReport."""
        report = DiagnosticReport(
            summary="Roundtrip test",
            issues_found=["x"],
            actions_taken=[
                RemediationAction(
                    host="h1", action="a", result="r", success=True
                )
            ],
            cluster_healthy=True,
        )
        resp = AgentRunResponse(
            output=report.model_dump(),
            output_type="DiagnosticReport",
            usage={"input_tokens": 1, "output_tokens": 2},
            agent_name="diag",
            success=True,
        )
        reconstructed = DiagnosticReport.model_validate(resp.output)
        assert reconstructed.summary == "Roundtrip test"
        assert reconstructed.actions_taken[0].host == "h1"
