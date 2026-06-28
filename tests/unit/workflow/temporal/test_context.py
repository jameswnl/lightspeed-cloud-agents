"""Unit tests for sandbox context building (TDD — RED phase)."""

from __future__ import annotations

from agents.workflow.temporal_context import build_sandbox_context
from agents.workflow.temporal_models import StepResult


class TestContextBuilding:
    """Tests for build_sandbox_context function."""

    def test_empty_steps_returns_minimal_context(self) -> None:
        """No prior steps produces empty context sections."""
        ctx = build_sandbox_context(
            workflow_steps={},
            current_step={"name": "step1", "prompt": "diagnose"},
        )
        assert ctx == {}

    def test_previous_attempts_from_failed_steps(self) -> None:
        """Failed prior steps produce previousAttempts context."""
        steps = {
            "r1": StepResult(
                status="failed",
                output={"summary": "timeout connecting"},
                error="retries exhausted",
            ),
        }
        ctx = build_sandbox_context(
            workflow_steps=steps,
            current_step={"name": "step2", "prompt": "retry", "role": "execution"},
        )
        assert "previousAttempts" in ctx
        assert len(ctx["previousAttempts"]) == 1
        assert ctx["previousAttempts"][0]["step"] == "r1"
        assert ctx["previousAttempts"][0]["error"] == "retries exhausted"

    def test_approved_option_by_id(self) -> None:
        """Approval step's selected_option_id resolves the correct option."""
        steps = {
            "analysis": StepResult(
                status="completed",
                output={
                    "options": [
                        {"id": "opt-1", "action": "restart pod"},
                        {"id": "opt-2", "action": "scale replicas"},
                    ],
                },
            ),
            "approval": StepResult(
                status="completed",
                output={"approved": True, "selected_option_id": "opt-2"},
            ),
        }
        ctx = build_sandbox_context(
            workflow_steps=steps,
            current_step={
                "name": "execute",
                "prompt": "apply fix",
                "role": "execution",
                "approval_step": "approval",
                "analysis_step": "analysis",
            },
        )
        assert "approvedOption" in ctx
        assert ctx["approvedOption"]["id"] == "opt-2"
        assert ctx["approvedOption"]["action"] == "scale replicas"

    def test_approved_option_fallback_to_first(self) -> None:
        """Missing selected_option_id falls back to first option."""
        steps = {
            "analysis": StepResult(
                status="completed",
                output={
                    "options": [{"id": "opt-1", "action": "restart"}],
                },
            ),
            "approval": StepResult(
                status="completed",
                output={"approved": True},
            ),
        }
        ctx = build_sandbox_context(
            workflow_steps=steps,
            current_step={
                "name": "execute",
                "role": "execution",
                "prompt": "apply",
                "approval_step": "approval",
                "analysis_step": "analysis",
            },
        )
        assert ctx["approvedOption"]["id"] == "opt-1"

    def test_execution_result_for_verification_step(self) -> None:
        """Verification step receives executionResult context."""
        steps = {
            "exec": StepResult(
                status="completed",
                output={"commands_run": ["kubectl rollout restart deploy/api"]},
            ),
        }
        ctx = build_sandbox_context(
            workflow_steps=steps,
            current_step={
                "name": "verify",
                "role": "verification",
                "prompt": "verify fix",
                "execution_step": "exec",
            },
        )
        assert "executionResult" in ctx
        assert ctx["executionResult"]["commands_run"] == [
            "kubectl rollout restart deploy/api"
        ]

    def test_target_namespaces_from_step_config(self) -> None:
        """Target namespaces passed through from step config."""
        ctx = build_sandbox_context(
            workflow_steps={},
            current_step={
                "name": "diag",
                "prompt": "check",
                "target_namespaces": ["production", "staging"],
            },
        )
        assert ctx["targetNamespaces"] == ["production", "staging"]

    def test_no_target_namespaces_omitted(self) -> None:
        """Missing target_namespaces doesn't add the key."""
        ctx = build_sandbox_context(
            workflow_steps={},
            current_step={"name": "diag", "prompt": "check"},
        )
        assert "targetNamespaces" not in ctx
