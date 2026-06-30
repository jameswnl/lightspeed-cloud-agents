"""Unit tests for workflow definition updates for Temporal/sandbox (TDD)."""

from __future__ import annotations

from cloud_agents.workflow.definition import (
    WorkflowStepSpec,
)


class TestStepSpecNewFields:
    """Tests for new WorkflowStepSpec fields."""

    def test_runtime_default_is_sandbox(self) -> None:
        """Default runtime is sandbox."""
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
        )
        assert step.runtime == "sandbox"

    def test_runtime_generic(self) -> None:
        """Generic runtime is accepted."""
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
            runtime="generic",
        )
        assert step.runtime == "generic"

    def test_role_field(self) -> None:
        """Role field accepts valid values."""
        for role in ("analysis", "execution", "verification"):
            step = WorkflowStepSpec(
                name="s1",
                type="agent",
                output_key="r1",
                prompt="check",
                role=role,
            )
            assert step.role == role

    def test_role_default_none(self) -> None:
        """Role defaults to None."""
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
        )
        assert step.role is None

    def test_instructions_field(self) -> None:
        """Inline instructions field."""
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
            instructions="You are a K8s diagnostic agent.",
        )
        assert step.instructions == "You are a K8s diagnostic agent."

    def test_output_schema_field(self) -> None:
        """Output schema for structured agent responses."""
        schema = {"type": "object", "properties": {"fix": {"type": "string"}}}
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
            output_schema=schema,
        )
        assert step.output_schema == schema

    def test_target_namespaces_field(self) -> None:
        """Target namespaces for sandbox scope."""
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
            target_namespaces=["production", "staging"],
        )
        assert step.target_namespaces == ["production", "staging"]

    def test_service_account_field(self) -> None:
        """Per-step service account for RBAC."""
        step = WorkflowStepSpec(
            name="s1",
            type="agent",
            output_key="r1",
            prompt="check",
            service_account="diag-sa",
        )
        assert step.service_account == "diag-sa"
