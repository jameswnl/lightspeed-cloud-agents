"""Unit tests for retry context and escalation."""

import pytest

from agents.workflow.retry import (
    EscalationHandoff,
    RetryContext,
    build_escalation,
)


class TestRetryContext:
    """Tests for RetryContext."""

    def test_initial_state(self) -> None:
        """Test initial retry context."""
        ctx = RetryContext()
        assert ctx.attempt == 1
        assert ctx.max_attempts == 3
        assert ctx.history == []
        assert not ctx.exhausted

    def test_add_failure(self) -> None:
        """Test recording a failure increments attempt."""
        ctx = RetryContext()
        ctx.add_failure("LLM error", {"partial": "data"})
        assert ctx.attempt == 2
        assert len(ctx.history) == 1
        assert ctx.history[0]["error"] == "LLM error"

    def test_exhausted_after_max_attempts(self) -> None:
        """Test that context is exhausted after max attempts."""
        ctx = RetryContext(max_attempts=2)
        ctx.add_failure("fail 1")
        ctx.add_failure("fail 2")
        assert ctx.exhausted

    def test_not_exhausted_before_max(self) -> None:
        """Test that context is not exhausted before max."""
        ctx = RetryContext(max_attempts=3)
        ctx.add_failure("fail 1")
        assert not ctx.exhausted

    def test_build_retry_prompt_includes_history(self) -> None:
        """Test that retry prompt includes failure history."""
        ctx = RetryContext(max_attempts=3)
        ctx.add_failure("Connection refused")
        prompt = ctx.build_retry_prompt("Diagnose the cluster")
        assert "Diagnose the cluster" in prompt
        assert "Connection refused" in prompt
        assert "attempt 2 of 3" in prompt

    def test_build_retry_prompt_multiple_failures(self) -> None:
        """Test retry prompt with multiple prior failures."""
        ctx = RetryContext(max_attempts=3)
        ctx.add_failure("Error 1")
        ctx.add_failure("Error 2")
        prompt = ctx.build_retry_prompt("Fix it")
        assert "Attempt 1: FAILED" in prompt
        assert "Attempt 2: FAILED" in prompt
        assert "attempt 3 of 3" in prompt

    def test_custom_max_attempts(self) -> None:
        """Test configurable max attempts."""
        ctx = RetryContext(max_attempts=5)
        assert ctx.max_attempts == 5


class TestBuildEscalation:
    """Tests for build_escalation."""

    def test_basic_escalation(self) -> None:
        """Test building an escalation handoff."""
        ctx = RetryContext(max_attempts=2)
        ctx.add_failure("Agent timeout")
        ctx.add_failure("LLM refused")

        handoff = build_escalation(
            workflow_name="cluster-rca",
            step_name="diagnose",
            retry_ctx=ctx,
        )

        assert handoff.workflow_name == "cluster-rca"
        assert handoff.step_name == "diagnose"
        assert handoff.attempts == 2
        assert len(handoff.failure_history) == 2
        assert handoff.failure_history[0]["error"] == "Agent timeout"

    def test_escalation_with_evidence(self) -> None:
        """Test escalation with collected evidence."""
        ctx = RetryContext(max_attempts=1)
        ctx.add_failure("Failed")

        handoff = build_escalation(
            workflow_name="rca",
            step_name="fix",
            retry_ctx=ctx,
            collected_evidence={"hosts_checked": ["web-01", "web-02"]},
        )

        assert handoff.evidence["hosts_checked"] == ["web-01", "web-02"]

    def test_escalation_json_serializable(self) -> None:
        """Test that escalation handoff serializes to JSON."""
        ctx = RetryContext(max_attempts=1)
        ctx.add_failure("err")
        handoff = build_escalation("wf", "step", ctx)
        json_str = handoff.model_dump_json()
        restored = EscalationHandoff.model_validate_json(json_str)
        assert restored.step_name == "step"

    def test_escalation_default_recommendation(self) -> None:
        """Test default recommendation text."""
        ctx = RetryContext(max_attempts=1)
        ctx.add_failure("err")
        handoff = build_escalation("wf", "step", ctx)
        assert "Manual investigation" in handoff.recommendation
