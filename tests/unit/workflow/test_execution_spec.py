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


class TestPrecedenceChain:
    """Service (run) → workflow-definition → step precedence (issue #268).

    Run-level provider and sandbox image are defaults only: a workflow
    definition value outranks them, and a step value outranks both. Both
    runners resolve through this same chain.
    """

    RUN_PROVIDER = {"name": "openai", "model": "gpt-4o", "credentials_secret": "run-key"}

    def _build(
        self,
        step: dict,
        workflow_defaults: dict | None = None,
    ):
        from cloud_agents.workflow.core.execution import build_step_input

        return build_step_input(
            step,
            run_context={
                "provider": dict(self.RUN_PROVIDER),
                "sandbox_image": "run-image",
                "workflow_id": "wf-prec",
            },
            workflow_defaults=workflow_defaults,
        )

    def test_definition_provider_outranks_run_provider(self) -> None:
        """The definition-level provider wins over the run-level default."""
        step_input = self._build(
            {"name": "s1", "output_key": "r1", "prompt": "p"},
            workflow_defaults={"provider": {"name": "openai", "model": "gpt-4o-mini"}},
        )
        assert step_input.provider["name"] == "openai"
        assert step_input.provider["model"] == "gpt-4o-mini"

    def test_cross_name_workflow_provider_is_rejected(self) -> None:
        """A workflow provider cannot cross the run authorization boundary."""
        with pytest.raises(ValueError, match="cross-provider"):
            self._build(
                {"name": "s1", "output_key": "r1", "prompt": "p"},
                workflow_defaults={"provider": {"name": "claude", "model": "claude-sonnet"}},
            )

    def test_run_credentials_bind_same_name_definition_provider(self) -> None:
        """Same-name definition provider inherits the run credential ref."""
        step_input = self._build(
            {"name": "s1", "output_key": "r1", "prompt": "p"},
            workflow_defaults={"provider": {"name": "openai", "model": "gpt-4o-mini"}},
        )
        assert step_input.provider["model"] == "gpt-4o-mini"
        assert step_input.provider["credentials_secret"] == "run-key"

    def test_step_provider_outranks_definition_and_run(self) -> None:
        """A step-level override outranks definition and run levels."""
        step_input = self._build(
            {
                "name": "s1",
                "output_key": "r1",
                "prompt": "p",
                "inference_provider": {"name": "openai", "model": "gpt-4o-mini"},
            },
            workflow_defaults={"provider": {"name": "openai", "model": "gpt-4o"}},
        )
        assert step_input.provider["name"] == "openai"
        assert step_input.provider["model"] == "gpt-4o-mini"

    def test_run_provider_used_when_definition_has_none(self) -> None:
        """The run-level provider is the default when the definition has none."""
        step_input = self._build({"name": "s1", "output_key": "r1", "prompt": "p"})
        assert step_input.provider["name"] == "openai"
        assert step_input.provider["credentials_secret"] == "run-key"

    def test_sandbox_image_step_over_definition_over_run(self) -> None:
        """Sandbox image precedence: step > definition > run."""
        defaults = {"spawn_config": {"sandbox_image": "img-def"}}
        step = {
            "name": "s1",
            "output_key": "r1",
            "prompt": "p",
            "spawn_config": {"sandbox_image": "img-step"},
        }
        assert self._build(step, defaults).sandbox_image == "img-step"

    def test_sandbox_image_definition_outranks_run(self) -> None:
        """Definition-level spawn_config image wins over the run image."""
        defaults = {"spawn_config": {"sandbox_image": "img-def"}}
        step = {"name": "s1", "output_key": "r1", "prompt": "p"}
        assert self._build(step, defaults).sandbox_image == "img-def"

    def test_sandbox_image_step_config_without_image_keeps_definition(self) -> None:
        """A step spawn_config lacking an image falls to the definition image."""
        step = {
            "name": "s1",
            "output_key": "r1",
            "prompt": "p",
            "spawn_config": {"cpu_request": "200m"},
        }
        defaults = {"spawn_config": {"sandbox_image": "img-def"}}
        assert self._build(step, defaults).sandbox_image == "img-def"

    def test_sandbox_image_run_used_when_definition_has_none(self) -> None:
        """The run-level image is the default when the definition has none."""
        step_input = self._build({"name": "s1", "output_key": "r1", "prompt": "p"})
        assert step_input.sandbox_image == "run-image"


class TestExecutionContextDelivery:
    """User-supplied execution context reaches the executor (issue #268).

    ``execution_context`` is distinct from prior step results: the
    workflow+step merged context is delivered on its own StepInput field
    and prior outputs stay in ``context``.
    """

    def _build(self, step: dict, workflow_defaults: dict | None = None):
        from cloud_agents.workflow.core.execution import build_step_input

        return build_step_input(
            step,
            run_context={
                "provider": {"name": "openai", "model": "gpt-4o"},
                "step_results": {"prev": {"status": "completed", "output": {"r": 1}}},
                "workflow_id": "wf-exec-ctx",
            },
            workflow_defaults=workflow_defaults,
        )

    def test_merged_context_flows_to_execution_context(self) -> None:
        """Workflow+step context merges onto execution_context."""
        step_input = self._build(
            {"name": "s1", "output_key": "r1", "prompt": "p", "context": {"env": "staging"}},
            workflow_defaults={"context": {"region": "eu"}},
        )
        assert step_input.execution_context == {"region": "eu", "env": "staging"}

    def test_step_context_overrides_workflow_context(self) -> None:
        """Step values win per key over workflow values."""
        step_input = self._build(
            {"name": "s1", "output_key": "r1", "prompt": "p", "context": {"env": "qa"}},
            workflow_defaults={"context": {"env": "prod", "region": "eu"}},
        )
        assert step_input.execution_context == {"env": "qa", "region": "eu"}

    def test_prior_results_stay_out_of_execution_context(self) -> None:
        """Prior step outputs land in context, never execution_context."""
        step_input = self._build({"name": "s1", "output_key": "r1", "prompt": "p"})
        assert step_input.context == {"prev": {"status": "completed", "output": {"r": 1}}}
        assert step_input.execution_context == {}

    def test_definition_models_carry_context(self) -> None:
        """WorkflowStepSpec/WorkflowSpec round-trip step/workflow context."""
        from cloud_agents.workflow.core.definition import WorkflowDefinition

        defn = WorkflowDefinition.model_validate(
            {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "ctx"},
                "spec": {
                    "context": {"region": "eu"},
                    "steps": [
                        {
                            "name": "s1",
                            "type": "agent",
                            "prompt": "p",
                            "output_key": "r1",
                            "context": {"env": "qa"},
                        }
                    ],
                },
            }
        )
        assert defn.spec.context == {"region": "eu"}
        assert defn.spec.steps[0].context == {"env": "qa"}


class TestProviderSpecStrictFields:
    """Unknown keys in provider mappings are rejected, not ignored (#270)."""

    def test_inference_provider_spec_rejects_unknown_fields(self) -> None:
        """A smuggled credentials_secret key fails validation."""
        with pytest.raises(ValidationError):
            InferenceProviderSpec.model_validate(
                {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"}
            )

    def test_normalize_rejects_step_provider_extra_keys(self) -> None:
        """Step provider mappings with unknown keys fail normalization."""
        from cloud_agents.workflow.core.execution import normalize_workflow_step

        with pytest.raises(ValueError, match="credentials_secret"):
            normalize_workflow_step(
                {
                    "name": "s1",
                    "output_key": "r1",
                    "prompt": "p",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4o",
                        "credentials_secret": "OPENAI_API_KEY",
                    },
                }
            )


class TestMcpDefaultParity:
    """Workflow-level MCP defaults flow and reject identically (#270)."""

    def test_workflow_mcp_default_reaches_step_input(self) -> None:
        """A step without mcp_servers inherits the spec-level catalog."""
        from cloud_agents.workflow.core.execution import build_step_input

        step_input = build_step_input(
            {"name": "s1", "output_key": "r1", "prompt": "p"},
            run_context={"provider": {"name": "openai", "model": "gpt-4o"}},
            workflow_defaults={
                "mcp_servers": [{"name": "cat", "url": "https://internal/x"}]
            },
        )
        resolved = step_input.mcp_servers
        assert resolved is not None and len(resolved) == 1
        assert resolved[0]["name"] == "cat"
        assert resolved[0]["url"] == "https://internal/x"

    def test_runtime_rejects_secret_in_workflow_mcp_default(self) -> None:
        """Normalization rejects a secret-bearing spec-level catalog."""
        from cloud_agents.workflow.core.execution import build_step_input

        with pytest.raises(ValueError, match="credentialed"):
            build_step_input(
                {"name": "s1", "output_key": "r1", "prompt": "p"},
                run_context={"provider": {"name": "openai", "model": "gpt-4o"}},
                workflow_defaults={
                    "mcp_servers": [{"name": "x", "url": "https://t:a@h/p"}]
                },
            )


class TestStoredStepProvider:
    """Step-level provider overrides survive stored definitions (#270 F5)."""

    def test_stored_definition_keeps_step_inference_provider(self) -> None:
        """WorkflowStepSpec round-trips inference_provider."""
        from cloud_agents.workflow.core.definition import WorkflowDefinition

        defn = WorkflowDefinition.model_validate(
            {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "stored"},
                "spec": {
                    "steps": [
                        {
                            "name": "s1",
                            "type": "agent",
                            "prompt": "p",
                            "output_key": "r1",
                            "inference_provider": {"name": "claude", "model": "sonnet"},
                        }
                    ]
                },
            }
        )
        step = defn.spec.steps[0]
        assert step.inference_provider is not None
        assert step.inference_provider.name == "claude"
        assert step.inference_provider.model == "sonnet"


class TestParallelDependencySafety:
    """Parallel groups do not race steps with prompt dependencies."""

    def test_internal_prompt_dependency_serializes_group(self) -> None:
        """A dependent member cannot run before its referenced output exists."""
        from cloud_agents.workflow.core.execution import chunk_parallel_groups

        chunks = chunk_parallel_groups(
            [
                {"name": "first", "parallel_group": "g", "prompt": "collect"},
                {
                    "name": "second",
                    "parallel_group": "g",
                    "prompt": "use {{ steps.first.output.value }}",
                },
            ]
        )
        assert chunks == [(None, [{"name": "first", "parallel_group": "g", "prompt": "collect"}]), (None, [{"name": "second", "parallel_group": "g", "prompt": "use {{ steps.first.output.value }}"}])]

    def test_output_key_and_condition_dependencies_serialize_group(self) -> None:
        """Output-key and condition references also impose ordering."""
        from cloud_agents.workflow.core.execution import chunk_parallel_groups

        chunks = chunk_parallel_groups(
            [
                {"name": "first", "output_key": "diagnosis", "parallel_group": "g"},
                {
                    "name": "second",
                    "output_key": "fix",
                    "parallel_group": "g",
                    "condition": "steps.diagnosis.output.ready",
                },
            ]
        )
        assert len(chunks) == 2
        assert all(group is None for group, _ in chunks)
