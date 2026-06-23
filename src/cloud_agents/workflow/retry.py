"""Context-aware retry and escalation for workflow steps.

Passes full failure history to retries. Generates an escalation
handoff document when retries are exhausted.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.workflow.state import StepResult


class RetryContext(BaseModel):
    """Context accumulated across retry attempts.

    Attributes:
        attempt: Current attempt number (1-based).
        max_attempts: Maximum allowed attempts.
        history: List of prior attempt results.
    """

    attempt: int = 1
    max_attempts: int = 3
    history: list[dict[str, Any]] = Field(default_factory=list)

    def add_failure(self, error: str, output: dict[str, Any] | None = None) -> None:
        """Record a failed attempt."""
        self.history.append({
            "attempt": self.attempt,
            "error": error,
            "output": output,
        })
        self.attempt += 1

    @property
    def exhausted(self) -> bool:
        """Whether all retry attempts have been used."""
        return self.attempt > self.max_attempts

    def build_retry_prompt(self, original_prompt: str) -> str:
        """Build a prompt that includes failure history for the next attempt.

        Args:
            original_prompt: The original step prompt.

        Returns:
            Enriched prompt with failure context.
        """
        history_text = "\n".join(
            f"Attempt {h['attempt']}: FAILED — {h['error']}"
            for h in self.history
        )
        return (
            f"{original_prompt}\n\n"
            f"PREVIOUS ATTEMPTS (all failed):\n{history_text}\n\n"
            f"This is attempt {self.attempt} of {self.max_attempts}. "
            f"Try a different approach based on what failed before."
        )


class EscalationHandoff(BaseModel):
    """Handoff document generated when retries are exhausted.

    Attributes:
        workflow_name: Name of the workflow that failed.
        step_name: The step that could not be completed.
        attempts: Number of attempts made.
        failure_history: Full history of what was tried and why it failed.
        evidence: Any outputs collected during attempts.
        recommendation: Suggested next steps for the human responder.
    """

    workflow_name: str
    step_name: str
    attempts: int
    failure_history: list[dict[str, Any]]
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommendation: str = "Manual investigation required. See failure history for context."


def build_escalation(
    workflow_name: str,
    step_name: str,
    retry_ctx: RetryContext,
    collected_evidence: dict[str, Any] | None = None,
) -> EscalationHandoff:
    """Build an escalation handoff document from retry context.

    Args:
        workflow_name: Name of the workflow.
        step_name: The step that failed.
        retry_ctx: Retry context with failure history.
        collected_evidence: Any evidence gathered during the workflow.

    Returns:
        Structured escalation handoff.
    """
    return EscalationHandoff(
        workflow_name=workflow_name,
        step_name=step_name,
        attempts=retry_ctx.attempt - 1,
        failure_history=retry_ctx.history,
        evidence=collected_evidence or {},
    )
