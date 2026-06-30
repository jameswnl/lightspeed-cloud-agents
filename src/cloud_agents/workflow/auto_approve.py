"""Policy-driven auto-approval for workflow steps.

Classifies workflow steps by risk level and auto-approves low-risk
steps without human intervention.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from agents.workflow.definition import WorkflowStepSpec

logger = logging.getLogger(__name__)


class ApprovalPolicy(BaseModel):
    """Policy for auto-approving workflow steps.

    Attributes:
        auto_approve_risk_levels: Risk levels that are auto-approved.
        default_risk: Default risk level for steps without explicit risk.
    """

    auto_approve_risk_levels: list[Literal["low", "medium", "high", "critical"]] = (
        Field(default_factory=lambda: ["low"])
    )
    default_risk: Literal["low", "medium", "high", "critical"] = "medium"


class StepRiskClassification(BaseModel):
    """Risk classification for a workflow step.

    Attributes:
        step_name: Name of the step.
        risk_level: Classified risk level.
        reason: Why this risk level was assigned.
        auto_approved: Whether the step is auto-approved by policy.
    """

    step_name: str
    risk_level: Literal["low", "medium", "high", "critical"]
    reason: str
    auto_approved: bool


def classify_step_risk(
    step: WorkflowStepSpec,
    policy: ApprovalPolicy,
) -> StepRiskClassification:
    """Classify a workflow step's risk level.

    Risk classification rules:
    - human-approval steps: always classified as the step's explicit risk or default
    - agent steps with "read" tools only: low risk
    - agent steps with "write/modify" tools: medium or higher
    - agent steps with remediation/execute in name: high risk

    Args:
        step: The workflow step specification.
        policy: The approval policy to apply.

    Returns:
        Risk classification with auto-approve decision.
    """
    if step.type == "human-approval":
        risk = step.risk_level or policy.default_risk
        reason = (
            "Human approval step — uses explicit risk level"
            if step.risk_level
            else "Human approval step — uses default risk level"
        )
    elif step.type == "agent":
        risk = _classify_agent_risk(step, policy)
        reason = _explain_agent_risk(step, risk)
    else:
        risk = policy.default_risk
        reason = "Unknown step type — uses default risk level"

    auto_approved = risk in policy.auto_approve_risk_levels

    return StepRiskClassification(
        step_name=step.name,
        risk_level=risk,
        reason=reason,
        auto_approved=auto_approved,
    )


def _classify_agent_risk(
    step: WorkflowStepSpec,
    policy: ApprovalPolicy,
) -> Literal["low", "medium", "high", "critical"]:
    """Classify risk for an agent step.

    Uses explicit risk_level from the step spec when set.
    Fails closed (defaults to "high") when not set.
    """
    if step.risk_level:
        return step.risk_level

    logger.warning(
        "Step '%s' has no explicit risk_level — defaulting to 'high' "
        "(manual approval required). Set risk_level in workflow YAML.",
        step.name,
    )
    return "high"


def _explain_agent_risk(
    step: WorkflowStepSpec,
    risk: str,
) -> str:
    """Generate a human-readable explanation for the risk classification."""
    if risk == "low":
        return (
            f"Step '{step.name}' classified as low risk — read/analysis operations only"
        )
    if risk == "high":
        return f"Step '{step.name}' classified as high risk — contains modification/execution keywords"
    return f"Step '{step.name}' classified as {risk} risk — default classification"
