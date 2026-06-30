"""Unit tests for policy-driven auto-approval."""

from cloud_agents.workflow.auto_approve import (
    ApprovalPolicy,
    classify_step_risk,
)
from cloud_agents.workflow.definition import WorkflowStepSpec


def _make_step(
    name: str, type: str = "agent", prompt: str = "", risk_level: str | None = None
) -> WorkflowStepSpec:
    return WorkflowStepSpec(
        name=name,
        type=type,
        prompt=prompt,
        output_key=f"{name}_result",
        spawn="pre-deployed",
        risk_level=risk_level,
    )


class TestClassifyStepRisk:
    """Tests for classify_step_risk."""

    def test_explicit_low_risk(self) -> None:
        """Test that explicit risk_level=low is used directly."""
        step = _make_step("diagnose", risk_level="low")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "low"
        assert result.auto_approved is True

    def test_explicit_high_risk(self) -> None:
        """Test that explicit risk_level=high blocks auto-approval."""
        step = _make_step("execute", risk_level="high")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "high"
        assert result.auto_approved is False

    def test_explicit_critical_risk(self) -> None:
        """Test that critical risk blocks auto-approval."""
        step = _make_step("delete-cluster", risk_level="critical")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "critical"
        assert result.auto_approved is False

    def test_no_risk_level_defaults_to_high(self) -> None:
        """Test fail-closed: missing risk_level defaults to high."""
        step = _make_step("something-unknown")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "high"
        assert result.auto_approved is False

    def test_human_approval_uses_default(self) -> None:
        """Test that human-approval steps use default risk."""
        step = _make_step("approve", type="human-approval")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.auto_approved is False

    def test_custom_auto_approve_levels(self) -> None:
        """Test custom auto-approve policy (auto-approve low + medium)."""
        policy = ApprovalPolicy(auto_approve_risk_levels=["low", "medium"])
        step = _make_step("generic", risk_level="medium")
        result = classify_step_risk(step, policy)
        assert result.risk_level == "medium"
        assert result.auto_approved is True

    def test_strict_policy_nothing_auto_approved(self) -> None:
        """Test strict policy that auto-approves nothing."""
        policy = ApprovalPolicy(auto_approve_risk_levels=[])
        step = _make_step("check", risk_level="low")
        result = classify_step_risk(step, policy)
        assert result.risk_level == "low"
        assert result.auto_approved is False

    def test_classification_includes_reason(self) -> None:
        """Test that classification includes a human-readable reason."""
        step = _make_step("diagnose", risk_level="low")
        result = classify_step_risk(step, ApprovalPolicy())
        assert "low risk" in result.reason
        assert "diagnose" in result.reason

    def test_explicit_overrides_name(self) -> None:
        """Test that explicit risk_level takes precedence even if name suggests otherwise."""
        step = _make_step("check-and-delete", risk_level="critical")
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "critical"
