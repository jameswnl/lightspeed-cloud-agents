"""Unit tests for policy-driven auto-approval."""

import pytest

from agents.workflow.auto_approve import (
    ApprovalPolicy,
    StepRiskClassification,
    classify_step_risk,
)
from agents.workflow.definition import WorkflowStepSpec


def _make_step(name: str, type: str = "agent", prompt: str = "") -> WorkflowStepSpec:
    return WorkflowStepSpec(
        name=name, type=type, prompt=prompt, output_key=f"{name}_result",
    )


class TestClassifyStepRisk:
    """Tests for classify_step_risk."""

    def test_diagnostic_step_is_low_risk(self) -> None:
        """Test that a diagnostic/analysis step is classified as low risk."""
        step = _make_step("diagnose", prompt="Diagnose the cluster issues")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "low"
        assert result.auto_approved is True

    def test_execute_step_is_high_risk(self) -> None:
        """Test that an execution step is classified as high risk."""
        step = _make_step("execute", prompt="Execute the remediation plan")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "high"
        assert result.auto_approved is False

    def test_restart_step_is_high_risk(self) -> None:
        """Test that a restart step is high risk."""
        step = _make_step("restart-service", prompt="Restart the app service")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "high"

    def test_check_step_is_low_risk(self) -> None:
        """Test that a check step is low risk."""
        step = _make_step("health-check", prompt="Check all hosts for issues")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "low"

    def test_unknown_step_uses_default(self) -> None:
        """Test that an unknown step name uses default risk."""
        step = _make_step("something-else", prompt="Do something generic")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "medium"
        assert result.auto_approved is False

    def test_human_approval_uses_default(self) -> None:
        """Test that human-approval steps use default risk."""
        step = _make_step("approve", type="human-approval")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "medium"

    def test_custom_auto_approve_levels(self) -> None:
        """Test custom auto-approve policy (auto-approve low + medium)."""
        policy = ApprovalPolicy(auto_approve_risk_levels=["low", "medium"])
        step = _make_step("generic", prompt="Do something")
        result = classify_step_risk(step, policy)
        assert result.risk_level == "medium"
        assert result.auto_approved is True

    def test_strict_policy_nothing_auto_approved(self) -> None:
        """Test strict policy that auto-approves nothing."""
        policy = ApprovalPolicy(auto_approve_risk_levels=[])
        step = _make_step("check", prompt="Check hosts")
        result = classify_step_risk(step, policy)
        assert result.risk_level == "low"
        assert result.auto_approved is False

    def test_classification_includes_reason(self) -> None:
        """Test that classification includes a human-readable reason."""
        step = _make_step("diagnose", prompt="Diagnose issues")
        result = classify_step_risk(step, ApprovalPolicy())
        assert "low risk" in result.reason
        assert "diagnose" in result.reason

    def test_rollback_is_high_risk(self) -> None:
        """Test that rollback operations are high risk."""
        step = _make_step("rollback", prompt="Rollback the deployment")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "high"

    def test_verify_step_is_low_risk(self) -> None:
        """Test that verification steps are low risk."""
        step = _make_step("verify", prompt="Verify the fix worked")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "low"
