"""Unit tests for the canonical agent execution specification.

Covers issue #268: AgentWorkflow as the only public execution abstraction.
A standalone agent invocation becomes a one-step workflow; these models are
the single contract both shapes normalize to.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cloud_agents.workflow.core.execution import (
    ONE_STEP_NAME,
    ONE_STEP_OUTPUT_KEY,
    AgentExecutionSpec,
    InferenceProviderSpec,
    WorkflowAgentStep,
    apply_one_step_defaults,
)


class TestInferenceProviderSpec:
    """Tests for InferenceProviderSpec model."""

    def test_valid_spec(self) -> None:
        """Test that a valid provider spec validates."""
        spec = InferenceProviderSpec(name="openai", model="gpt-4o")
        assert spec.name == "openai"
        assert spec.model == "gpt-4o"

    def test_name_is_required(self) -> None:
        """Test that provider name is required."""
        with pytest.raises(ValidationError):
            InferenceProviderSpec(name="", model="gpt-4o")

    def test_model_is_required(self) -> None:
        """Test that model is required."""
        with pytest.raises(ValidationError):
            InferenceProviderSpec(name="openai", model="")


class TestAgentExecutionSpec:
    """Tests for AgentExecutionSpec model."""

    def test_minimal_spec(self) -> None:
        """Test that only prompt is required."""
        spec = AgentExecutionSpec(prompt="Inspect the cluster")
        assert spec.prompt == "Inspect the cluster"
        assert spec.instructions is None
        assert spec.inference_provider is None
        assert spec.tools == []
        assert spec.mcp_servers is None
        assert spec.allowed_skills is None
        assert spec.permissions is None
        assert spec.spawn == "ephemeral"
        assert spec.spawn_config is None
        assert spec.output_schema is None
        assert spec.context == {}
        assert spec.timeout_seconds is None

    def test_spawn_modes(self) -> None:
        """Test that all three spawn modes validate."""
        for mode in ("none", "local", "ephemeral"):
            spec = AgentExecutionSpec(prompt="hi", spawn=mode)  # type: ignore[arg-type]
            assert spec.spawn == mode

    def test_invalid_spawn_rejected(self) -> None:
        """Test that unknown spawn modes are rejected."""
        with pytest.raises(ValidationError):
            AgentExecutionSpec(prompt="hi", spawn="k8s")

    def test_timeout_must_be_positive(self) -> None:
        """Test that timeout_seconds rejects zero and negatives."""
        with pytest.raises(ValidationError):
            AgentExecutionSpec(prompt="hi", timeout_seconds=0)
        with pytest.raises(ValidationError):
            AgentExecutionSpec(prompt="hi", timeout_seconds=-5)

    def test_secret_fields_rejected(self) -> None:
        """Test that secret-bearing fields cannot enter the execution spec."""
        with pytest.raises(ValidationError):
            AgentExecutionSpec(prompt="hi", credentials_secret="my-secret")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            AgentExecutionSpec(prompt="hi", api_key="sk-live-123")  # type: ignore[call-arg]

    def test_none_means_inherit_for_optional_lists(self) -> None:
        """Test that None is preserved (inherit), distinct from [] (empty)."""
        spec = AgentExecutionSpec(prompt="hi")
        assert spec.mcp_servers is None
        assert spec.allowed_skills is None
        explicit_empty = AgentExecutionSpec(prompt="hi", mcp_servers=[], allowed_skills=[])
        assert explicit_empty.mcp_servers == []
        assert explicit_empty.allowed_skills == []


class TestWorkflowAgentStep:
    """Tests for WorkflowAgentStep model."""

    def test_requires_name_and_output_key(self) -> None:
        """Test that orchestration identity fields are required."""
        with pytest.raises(ValidationError):
            WorkflowAgentStep(prompt="hi")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            WorkflowAgentStep(prompt="hi", name="agent")  # type: ignore[call-arg]

    def test_valid_step(self) -> None:
        """Test that a fully specified step validates."""
        step = WorkflowAgentStep(
            name="agent",
            output_key="result",
            prompt="Inspect the cluster",
            tools=["kubectl_get"],
        )
        assert step.name == "agent"
        assert step.output_key == "result"
        assert step.tools == ["kubectl_get"]
        assert step.max_retries == 0
        assert step.parallel_group is None
        assert step.role is None
        assert step.condition is None

    def test_max_retries_counts_retries_after_initial(self) -> None:
        """Test that max_retries=0 means one total attempt (non-negative)."""
        step = WorkflowAgentStep(name="a", output_key="o", prompt="hi")
        assert step.max_retries == 0
        with pytest.raises(ValidationError):
            WorkflowAgentStep(name="a", output_key="o", prompt="hi", max_retries=-1)

    def test_role_is_advisory_metadata(self) -> None:
        """Test that role accepts the documented literals."""
        for role in ("analysis", "execution", "verification"):
            step = WorkflowAgentStep(
                name="a", output_key="o", prompt="hi", role=role  # type: ignore[arg-type]
            )
            assert step.role == role


class TestOneStepDefaults:
    """Tests for the one-step workflow naming convention."""

    def test_convention_constants(self) -> None:
        """Test the documented one-step name/output_key convention."""
        assert ONE_STEP_NAME == "agent"
        assert ONE_STEP_OUTPUT_KEY == "result"

    def test_missing_name_and_output_key_defaulted(self) -> None:
        """Test that a bare one-step dict gets the documented defaults."""
        result = apply_one_step_defaults({"prompt": "hi"})
        assert result["name"] == "agent"
        assert result["output_key"] == "result"
        assert result["prompt"] == "hi"

    def test_explicit_names_preserved(self) -> None:
        """Test that explicit names are not overwritten."""
        result = apply_one_step_defaults({"name": "custom", "output_key": "out", "prompt": "hi"})
        assert result["name"] == "custom"
        assert result["output_key"] == "out"

    def test_does_not_mutate_input(self) -> None:
        """Test that the input dict is not modified in place."""
        original = {"prompt": "hi"}
        apply_one_step_defaults(original)
        assert original == {"prompt": "hi"}

    def test_multi_step_requires_explicit_names(self) -> None:
        """Test that defaulting applies to single-step definitions only."""
        with pytest.raises(ValueError, match="one-step"):
            apply_one_step_defaults(
                {"prompt": "hi"},
                step_count=2,
            )
