"""Unit tests for canonical workflow-step normalization (issue #268).

Normalization is the single path both one-step and multi-step workflows
take before execution::

    WorkflowStepSpec dict
            ↓
    normalize and validate
            ↓
    WorkflowAgentStep (+ orchestration/legacy metadata)
            ↓
    StepInput

Precedence for inherited configuration::

    service defaults
            ↓
    workflow defaults
            ↓
    step-level overrides

``None`` means inherit; an explicit empty list means explicitly empty.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agents.workflow.core.execution import (
    ONE_STEP_NAME,
    ONE_STEP_OUTPUT_KEY,
    AgentExecutionSpec,
    InferenceProviderSpec,
    WorkflowAgentStep,
    build_step_input,
    collapse_permissions,
    inference_spec_from_provider_config,
    merge_context,
    normalize_definition,
    normalize_workflow_step,
    reject_secret_bearing_mcp,
    validate_credential_reference,
    validate_inference_provider,
)


def _step(**overrides: Any) -> dict[str, Any]:
    """Build a minimal agent step dict with optional overrides."""
    base: dict[str, Any] = {
        "name": "agent",
        "type": "agent",
        "prompt": "Inspect the cluster",
        "output_key": "result",
    }
    base.update(overrides)
    return base


class TestNormalizeWorkflowStep:
    """Tests for single-step normalization."""

    def test_minimal_step_normalizes(self) -> None:
        """Test that a minimal step yields a WorkflowAgentStep."""
        step, meta = normalize_workflow_step(_step())
        assert isinstance(step, WorkflowAgentStep)
        assert step.name == "agent"
        assert step.output_key == "result"
        assert step.prompt == "Inspect the cluster"
        assert meta["type"] == "agent"

    def test_one_step_defaults_applied(self) -> None:
        """Test that a bare one-step dict gets the documented convention."""
        step, _ = normalize_workflow_step({"prompt": "hi"}, step_count=1)
        assert step.name == ONE_STEP_NAME
        assert step.output_key == ONE_STEP_OUTPUT_KEY

    def test_multi_step_requires_explicit_names(self) -> None:
        """Test that bare steps are rejected in multi-step definitions."""
        with pytest.raises(ValueError, match="one-step"):
            normalize_workflow_step({"prompt": "hi"}, step_count=2)

    def test_human_approval_is_not_an_agent_step(self) -> None:
        """Test that human-approval steps cannot normalize to agent spec."""
        with pytest.raises(ValueError, match="human-approval"):
            normalize_workflow_step(
                _step(type="human-approval", prompt=None, message="approve?")
            )

    def test_unknown_type_rejected(self) -> None:
        """Test that unknown step types are rejected."""
        with pytest.raises(ValueError, match="type"):
            normalize_workflow_step(_step(type="shell"))

    def test_agent_reference_preserved_as_metadata(self) -> None:
        """Test that the named-agent reference is not silently dropped."""
        _, meta = normalize_workflow_step(_step(agent="cluster-expert"))
        assert meta["agent"] == "cluster-expert"

    def test_runtime_documented_as_metadata(self) -> None:
        """Test that legacy runtime is carried as metadata, not executed."""
        _, meta = normalize_workflow_step(_step(runtime="generic"))
        assert meta["runtime"] == "generic"

    def test_message_preserved_as_metadata(self) -> None:
        """Test that human-approval message text stays in metadata."""
        _, meta = normalize_workflow_step(_step(message="note"))
        assert meta["message"] == "note"

    def test_risk_level_preserved_as_audit_metadata(self) -> None:
        """Test that risk_level survives as audit metadata."""
        _, meta = normalize_workflow_step(_step(risk_level="high"))
        assert meta["risk_level"] == "high"

    def test_timeout_must_be_positive(self) -> None:
        """Test that zero/negative per-step timeouts are rejected."""
        with pytest.raises(ValueError, match="timeout_seconds"):
            normalize_workflow_step(_step(timeout_seconds=0))
        with pytest.raises(ValueError, match="timeout_seconds"):
            normalize_workflow_step(_step(timeout_seconds=-5))

    def test_step_names_must_be_unique_at_definition_level(self) -> None:
        """Test that duplicate step names fail definition normalization."""
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            normalize_definition({"spec": {"steps": [_step(), _step()]}})


class TestPrecedence:
    """Tests for service → workflow → step precedence."""

    def test_workflow_provider_default_applies(self) -> None:
        """Test that a workflow-level provider fills a step without one."""
        step, _ = normalize_workflow_step(
            _step(),
            workflow_defaults={"provider": {"name": "openai", "model": "gpt-4o"}},
        )
        assert step.inference_provider == InferenceProviderSpec(
            name="openai", model="gpt-4o"
        )

    def test_step_provider_overrides_workflow(self) -> None:
        """Test that a step-level provider wins over the workflow default."""
        step, _ = normalize_workflow_step(
            _step(
                inference_provider={"name": "claude", "model": "claude-sonnet"}
            ),
            workflow_defaults={"provider": {"name": "openai", "model": "gpt-4o"}},
        )
        assert step.inference_provider is not None
        assert step.inference_provider.name == "claude"

    def test_workflow_timeout_default_applies(self) -> None:
        """Test that a workflow-level timeout fills a step without one."""
        step, _ = normalize_workflow_step(
            _step(), workflow_defaults={"timeout_seconds": 120}
        )
        assert step.timeout_seconds == 120

    def test_step_timeout_overrides_workflow(self) -> None:
        """Test that an explicit step timeout wins."""
        step, _ = normalize_workflow_step(
            _step(timeout_seconds=30),
            workflow_defaults={"timeout_seconds": 120},
        )
        assert step.timeout_seconds == 30

    def test_workflow_spawn_default_applies(self) -> None:
        """Test that a workflow-level spawn mode fills a step without one."""
        step, _ = normalize_workflow_step(
            {"name": "a", "type": "agent", "prompt": "hi", "output_key": "o"},
            workflow_defaults={"spawn": "local"},
        )
        assert step.spawn == "local"

    def test_none_means_inherit_for_mcp_and_skills(self) -> None:
        """Test that omitted lists inherit workflow catalogs."""
        step, _ = normalize_workflow_step(
            _step(),
            workflow_defaults={
                "mcp_servers": ["catalog-a"],
                "allowed_skills": ["kubernetes"],
            },
        )
        assert step.mcp_servers == ["catalog-a"]
        assert step.allowed_skills == ["kubernetes"]

    def test_empty_list_means_explicitly_empty(self) -> None:
        """Test that [] overrides a workflow default with empty."""
        step, _ = normalize_workflow_step(
            _step(mcp_servers=[], allowed_skills=[]),
            workflow_defaults={
                "mcp_servers": ["catalog-a"],
                "allowed_skills": ["kubernetes"],
            },
        )
        assert step.mcp_servers == []
        assert step.allowed_skills == []

    def test_step_sandbox_image_overrides_service_default(self) -> None:
        """Test that spawn_config.sandbox_image wins over the run default."""
        step, _ = normalize_workflow_step(
            _step(spawn_config={"sandbox_image": "quay.io/example/sandbox:v2"}),
        )
        assert step.spawn_config is not None
        assert step.spawn_config.sandbox_image == "quay.io/example/sandbox:v2"


class TestProviderMapping:
    """Tests for ProviderConfig → InferenceProviderSpec mapping."""

    def test_credentials_secret_is_dropped(self) -> None:
        """Test that runtime credentials never enter the execution spec."""
        spec = inference_spec_from_provider_config(
            {"name": "openai", "model": "gpt-4o", "credentials_secret": "OPENAI_API_KEY"}
        )
        assert spec == InferenceProviderSpec(name="openai", model="gpt-4o")
        assert "credentials_secret" not in spec.model_dump()

    def test_model_dump_has_no_secret_keys(self) -> None:
        """Test that the serialized spec carries no secret-bearing keys."""
        spec = inference_spec_from_provider_config(
            {"name": "claude", "model": "claude-sonnet", "credentials_secret": "X"}
        )
        dumped = spec.model_dump()
        assert not any("secret" in key.lower() for key in dumped)

    def test_unapproved_provider_name_rejected(self) -> None:
        """Test that free-form provider names fail validation."""
        with pytest.raises(ValueError, match="[Pp]rovider"):
            validate_inference_provider({"name": "evil-proxy", "model": "x"})

    def test_approved_provider_names_accepted(self) -> None:
        """Test that executor-known provider names validate.

        The allowlist aliases the executor's supported names (B3: azure,
        bedrock, and anthropic are legitimate and must not be rejected);
        tenant authorization against the approved catalog is enforced
        stack-side.
        """
        for name in ("openai", "claude", "gemini", "anthropic", "azure", "bedrock"):
            spec = validate_inference_provider({"name": name, "model": "m"})
            assert spec.name == name

    def test_run_level_provider_needs_no_secret_reference(self) -> None:
        """Test that the run-level provider matches the issue #268 shape.

        The migration example uses ``{"name", "model"}`` with no
        ``credentials_secret``; runtime credentials travel separately
        (pre-#269: provider-default env keys; #269: injection contract).
        """
        from cloud_agents.workflow.core.models import ProviderConfig, WorkflowInput

        provider = ProviderConfig.model_validate({"name": "openai", "model": "gpt-4o"})
        assert provider.credentials_secret is None
        assert provider.model_dump(exclude_none=True) == {
            "name": "openai",
            "model": "gpt-4o",
        }
        workflow_input = WorkflowInput.model_validate(
            {
                "definition": {"kind": "AgentWorkflow", "spec": {"steps": []}},
                "workflow_id": "wf-1",
                "provider": {"name": "openai", "model": "gpt-4o"},
            }
        )
        assert workflow_input.provider.credentials_secret is None

    def test_workflow_definition_provider_needs_no_secret_reference(self) -> None:
        """Test that WorkflowDefinition accepts a secret-less provider."""
        from cloud_agents.workflow.core.definition import WorkflowDefinition

        defn = WorkflowDefinition.model_validate(
            {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "one-shot-agent"},
                "spec": {
                    "steps": [
                        {
                            "name": "agent",
                            "type": "agent",
                            "prompt": "Inspect the cluster",
                            "output_key": "result",
                        }
                    ]
                },
                "provider": {"name": "openai", "model": "gpt-4o"},
            }
        )
        assert defn.provider is not None
        assert defn.provider.credentials_secret is None

    def test_secret_less_run_provider_yields_bare_selection(self) -> None:
        """Test that build_step_input emits only name/model without a ref."""
        step_input = build_step_input(
            _step(),
            run_context={
                "provider": {"name": "openai", "model": "gpt-4o"},
                "sandbox_image": "sandbox:default",
            },
        )
        assert step_input.provider == {"name": "openai", "model": "gpt-4o"}


class TestCredentialReferenceValidation:
    """Tests for credentials_secret reference validation (issue #268, B1).

    The reference field must carry a secret *name*, never a value.
    """

    def test_valid_references_accepted(self) -> None:
        """Test that env keys and K8s-style names pass."""
        for ref in ("OPENAI_API_KEY", "openai-api-key", "k", "my-team.openai-key"):
            assert validate_credential_reference(ref) == ref

    def test_secret_values_rejected(self) -> None:
        """Test that secret-value shapes fail instead of flowing."""
        for bad in (
            "sk-" + "live-abc123",
            "Bearer " + "token123",
            "AKIA" + "IOSFODNN7EXAMPLE",
            "xoxb-" + "12345",
            "ghp_" + "abcdef",
            "-----BEGIN " + "PRIVATE KEY-----",
            "has whitespace",
            "",
        ):
            with pytest.raises(ValueError, match="[Ss]ecret|name"):
                validate_credential_reference(bad)

    def test_non_string_rejected(self) -> None:
        """Test that non-string references fail."""
        with pytest.raises(ValueError, match="[Ss]ecret|name"):
            validate_credential_reference(None)
        with pytest.raises(ValueError, match="[Ss]ecret|name"):
            validate_credential_reference({"secret_name": "x"})

    def test_provider_config_rejects_secret_value(self) -> None:
        """Test that the model boundary rejects values in the ref field."""
        import pydantic

        from cloud_agents.workflow.core.models import ProviderConfig

        with pytest.raises(pydantic.ValidationError):
            ProviderConfig.model_validate(
                {
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "sk-" + "live-abc123",
                }
            )

    def test_build_step_input_rejects_secret_value_ref(self) -> None:
        """Test that a value-shaped ref fails StepInput construction."""
        with pytest.raises(ValueError, match="[Ss]ecret"):
            build_step_input(
                _step(),
                run_context={
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4o",
                        "credentials_secret": "sk-" + "live-abc123",
                    },
                    "sandbox_image": "sandbox:default",
                },
            )


class TestOverrideCredentialDecoupling:
    """Tests that provider overrides never bind foreign creds (B2)."""

    def test_same_provider_override_keeps_reference(self) -> None:
        """Test that a same-provider override keeps the run-level ref."""
        step_input = build_step_input(
            _step(inference_provider={"name": "openai", "model": "o3"}),
            run_context={
                "provider": {
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "OPENAI_API_KEY",
                },
                "sandbox_image": "sandbox:default",
            },
        )
        assert step_input.provider["name"] == "openai"
        assert step_input.provider["model"] == "o3"
        assert step_input.provider["credentials_secret"] == "OPENAI_API_KEY"

    def test_cross_provider_override_is_rejected(self) -> None:
        """A nested provider cannot cross the run authorization boundary."""
        with pytest.raises(ValueError, match="cross-provider"):
            build_step_input(
                _step(inference_provider={"name": "azure", "model": "gpt-4o"}),
                run_context={
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4o",
                        "credentials_secret": "OPENAI_API_KEY",
                    },
                    "sandbox_image": "sandbox:default",
                },
            )


class TestMcpSecretRejection:
    """Tests for secret-bearing inline MCP configuration rejection."""

    def test_credentialed_url_rejected(self) -> None:
        """Test that URLs with embedded userinfo are rejected."""
        with pytest.raises(ValueError, match="[Ss]ecret|credential"):
            reject_secret_bearing_mcp(
                [{"name": "x", "url": "https://token:abc123@internal/x"}]
            )

    def test_schemeless_credentialed_url_rejected(self) -> None:
        """Test that scheme-less userinfo URLs are rejected (B6)."""
        with pytest.raises(ValueError, match="[Ss]ecret|credential"):
            reject_secret_bearing_mcp(
                [{"name": "x", "url": "tok:abc@internal/x"}]
            )

    def test_credential_shaped_header_value_rejected(self) -> None:
        """Test that credential-like values in benign headers fail (B6)."""
        with pytest.raises(ValueError, match="[Cc]redential"):
            reject_secret_bearing_mcp(
                [
                    {
                        "name": "x",
                        "url": "https://internal/x",
                        "headers": {"X-Custom": "sk-live-abc123"},
                    }
                ]
            )

    def test_benign_header_values_accepted(self) -> None:
        """Test that non-credential header values pass."""
        entry = {
            "name": "x",
            "url": "https://internal/x",
            "headers": {"X-Tenant-ID": "team-a"},
        }
        assert reject_secret_bearing_mcp([entry]) == [entry]

    def test_credentials_secret_key_in_entry_rejected(self) -> None:
        """Test that an ungated secret key inside an entry fails (B6)."""
        with pytest.raises(ValueError, match="credentials_secret"):
            reject_secret_bearing_mcp(
                [
                    {
                        "name": "x",
                        "url": "https://internal/x",
                        "credentials_secret": "SOME_REF",
                    }
                ]
            )

    def test_secret_references_defer_to_execution_allowlist(self) -> None:
        """Test that secret refs pass normalization (execution gates them).

        ``secret_headers`` carry references, not values. They execute only
        through the MCP_ALLOWED_SECRETS policy gate in the activity layer,
        which fails closed when unset -- normalization must not duplicate
        that gate by rejecting them.
        """
        entry = {
            "name": "x",
            "url": "https://internal/x",
            "secret_headers": {
                "Authorization": {"secret_name": "mcp-creds", "key": "token"}
            },
        }
        assert reject_secret_bearing_mcp([entry]) == [entry]

    def test_suspicious_header_names_rejected(self) -> None:
        """Test that secret-like header names fail without policy review."""
        with pytest.raises(ValueError, match="[Ss]ecret|[Hh]eader"):
            reject_secret_bearing_mcp(
                [
                    {
                        "name": "x",
                        "url": "https://internal/x",
                        "headers": {"Authorization": "Bearer abc"},
                    }
                ]
            )

    def test_plain_inline_config_accepted(self) -> None:
        """Test that a secret-free inline config passes."""
        result = reject_secret_bearing_mcp(
            [{"name": "x", "url": "https://internal/x"}]
        )
        assert result == [{"name": "x", "url": "https://internal/x"}]

    def test_catalog_names_accepted(self) -> None:
        """Test that catalog-name references pass (no values to scan)."""
        assert reject_secret_bearing_mcp(["catalog-a"]) == ["catalog-a"]
        assert reject_secret_bearing_mcp(None) is None

    def test_normalization_rejects_secret_mcp(self) -> None:
        """Test that normalize applies MCP secret rejection."""
        with pytest.raises(ValueError, match="[Ss]ecret|credential"):
            normalize_workflow_step(
                _step(
                    mcp_servers=[
                        {"name": "x", "url": "https://tok:abc@internal/x"}
                    ]
                )
            )


class TestPermissionCollapse:
    """Tests for legacy auth fields → PermissionScope normalization."""

    def test_legacy_service_account_fills_permissions(self) -> None:
        """Test that a legacy service_account becomes the scope."""
        scope, _ = collapse_permissions(
            permissions=None, service_account="agent-sa", risk_level="high"
        )
        assert scope is not None
        assert scope.service_account == "agent-sa"

    def test_explicit_permissions_preserved(self) -> None:
        """Test that explicit permissions survive unchanged."""
        from cloud_agents.workflow.core.permissions import PermissionScope

        scope, _ = collapse_permissions(
            permissions=PermissionScope(service_account="a"),
            service_account=None,
            risk_level=None,
        )
        assert scope is not None
        assert scope.service_account == "a"

    def test_conflicting_service_account_rejected(self) -> None:
        """Test that conflicting scopes fail instead of widening."""
        from cloud_agents.workflow.core.permissions import PermissionScope

        with pytest.raises(ValueError, match="[Cc]onflict|[Pp]ermission"):
            collapse_permissions(
                permissions=PermissionScope(service_account="a"),
                service_account="b",
                risk_level=None,
            )

    def test_risk_level_does_not_grant_permissions(self) -> None:
        """Test that risk_level alone yields no scope (audit only)."""
        scope, meta = collapse_permissions(
            permissions=None, service_account=None, risk_level="critical"
        )
        assert scope is None
        assert meta["risk_level"] == "critical"

    def test_target_namespaces_preserved_as_metadata(self) -> None:
        """Test that target_namespaces is carried, not dropped or widened."""
        _, meta = collapse_permissions(
            permissions=None,
            service_account=None,
            risk_level=None,
            target_namespaces=["team-a"],
        )
        assert meta["target_namespaces"] == ["team-a"]


class TestContextMerge:
    """Tests for workflow/step context merge semantics."""

    def test_merge_is_shallow_with_step_winning(self) -> None:
        """Test shallow merge: step values override workflow values."""
        merged = merge_context(
            {"a": 1, "nested": {"x": 1, "y": 2}}, {"nested": {"y": 99}, "b": 2}
        )
        assert merged == {"a": 1, "nested": {"y": 99}, "b": 2}

    def test_merge_does_not_mutate_inputs(self) -> None:
        """Test that neither input dict is modified."""
        workflow = {"a": 1}
        step = {"b": 2}
        merge_context(workflow, step)
        assert workflow == {"a": 1}
        assert step == {"b": 2}

    def test_step_context_overrides_workflow_in_normalization(self) -> None:
        """Test that step context wins during normalization."""
        step, _ = normalize_workflow_step(
            _step(context={"key": "step"}),
            workflow_defaults={"context": {"key": "workflow", "other": 1}},
        )
        assert step.context == {"key": "step", "other": 1}


class TestBuildStepInput:
    """Tests for the canonical StepInput construction helper."""

    def test_builds_step_input_with_run_defaults(self) -> None:
        """Test full construction: provider, image, MCP, interpolation."""
        step_input = build_step_input(
            _step(prompt="hello {{ steps.prior.output.text }}"),
            run_context={
                "provider": {
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "OPENAI_API_KEY",
                },
                "sandbox_image": "sandbox:default",
                "mcp_servers": [{"name": "catalog-a", "url": "https://a"}],
                "workflow_id": "wf-1",
                "step_results": {"prior": {"output": {"text": "world"}}},
            },
        )
        assert step_input.prompt == 'hello <data>"world"</data>'
        assert step_input.provider == {
            "name": "openai",
            "model": "gpt-4o",
            # Pre-#269 execution path: the secret *reference* flows so
            # ensure_credentials_env keeps working; values never do.
            "credentials_secret": "OPENAI_API_KEY",
        }
        assert step_input.sandbox_image == "sandbox:default"
        assert step_input.workflow_id == "wf-1"
        assert step_input.step_name == "agent"
        assert step_input.output_key == "result"

    def test_step_sandbox_image_wins_over_run_default(self) -> None:
        """Test that the step image override selects the executor image."""
        step_input = build_step_input(
            _step(spawn_config={"sandbox_image": "quay.io/example/sandbox:v9"}),
            run_context={
                "provider": {"name": "openai", "model": "gpt-4o"},
                "sandbox_image": "sandbox:default",
            },
        )
        assert step_input.sandbox_image == "quay.io/example/sandbox:v9"

    def test_step_input_serialization_has_no_secret_values(self) -> None:
        """Test that serialized StepInput carries references, not values."""
        step_input = build_step_input(
            _step(),
            run_context={
                "provider": {
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "OPENAI_API_KEY",
                },
                "sandbox_image": "sandbox:default",
            },
        )
        # Only the reference name plus selection flow to the executor.
        assert set(step_input.provider) == {
            "name",
            "model",
            "credentials_secret",
        }
        assert step_input.provider["credentials_secret"] == "OPENAI_API_KEY"
        # And the normalized execution spec itself carries no credential
        # reference at all (proved in TestProviderMapping).
        normalized, _ = normalize_workflow_step(_step())
        assert "credentials_secret" not in normalized.model_dump()

    def test_minimal_spec_requires_prompt(self) -> None:
        """Test that AgentExecutionSpec still requires a prompt."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            AgentExecutionSpec()  # type: ignore[call-arg]


class TestNormalizeDefinition:
    """Tests for whole-definition normalization."""

    def test_one_step_definition_normalizes(self) -> None:
        """Test that a one-step definition yields one normalized step."""
        steps, _ = normalize_definition(
            {
                "spec": {"steps": [{"prompt": "hi"}]},
                "provider": {"name": "openai", "model": "gpt-4o"},
            }
        )
        assert len(steps) == 1
        assert steps[0].name == ONE_STEP_NAME
        assert steps[0].inference_provider is not None
        assert steps[0].inference_provider.name == "openai"

    def test_multi_step_names_must_be_unique(self) -> None:
        """Test that multi-step collisions are rejected."""
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            normalize_definition(
                {"spec": {"steps": [_step(name="a"), _step(name="a")]}}
            )

    def test_human_approval_steps_pass_through_unnormalized(self) -> None:
        """Test that approval steps are returned as metadata, not agent spec."""
        steps, metas = normalize_definition(
            {
                "spec": {
                    "steps": [
                        _step(),
                        {
                            "name": "gate",
                            "type": "human-approval",
                            "message": "ok?",
                            "output_key": "gate_out",
                        },
                    ]
                }
            }
        )
        assert len(steps) == 1
        assert metas[1]["type"] == "human-approval"


class TestRemainingReviewFindings:
    """Regression tests for reviewer findings (N1/N2/S2/S5)."""

    def test_none_output_key_defaults_to_name(self) -> None:
        """Test that an explicit null output_key falls back to the name (N1)."""
        step, _ = normalize_workflow_step(_step(output_key=None))
        assert step.output_key == "agent"

    def test_bool_timeout_rejected(self) -> None:
        """Test that boolean timeouts are rejected (N2)."""
        with pytest.raises(ValueError, match="timeout_seconds"):
            normalize_workflow_step(_step(timeout_seconds=True))

    def test_workflow_level_service_account_applies(self) -> None:
        """Test that a workflow-level service account fills the scope (S2)."""
        step, _ = normalize_workflow_step(
            _step(), workflow_defaults={"service_account": "wf-sa"}
        )
        assert step.permissions is not None
        assert step.permissions.service_account == "wf-sa"

    def test_step_service_account_beats_workflow_default(self) -> None:
        """Test that a step service account wins over the workflow one."""
        step, _ = normalize_workflow_step(
            _step(service_account="step-sa"),
            workflow_defaults={"service_account": "wf-sa"},
        )
        assert step.permissions is not None
        assert step.permissions.service_account == "step-sa"

    def test_string_tools_rejected(self) -> None:
        """Test that a bare tools string fails loudly (S5)."""
        with pytest.raises(ValueError, match="tools"):
            normalize_workflow_step(_step(tools="kubectl_get"))

    def test_approval_agent_name_collision_rejected(self) -> None:
        """Test that an agent step and approval gate cannot share a name (N3)."""
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            normalize_definition(
                {
                    "spec": {
                        "steps": [
                            _step(name="shared"),
                            {
                                "name": "shared",
                                "type": "human-approval",
                                "message": "ok?",
                                "output_key": "gate_out",
                            },
                        ]
                    }
                }
            )
