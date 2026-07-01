"""Unit tests for Temporal workflow data models."""

import pytest

from cloud_agents.workflow.temporal_models import (
    ProviderConfig,
    SandboxStepInput,
    StepResult,
    WorkflowEvent,
    WorkflowInput,
    WorkflowOutput,
    WorkflowStatus,
)


class TestProviderConfig:
    """Tests for ProviderConfig model."""

    def test_valid_provider(self) -> None:
        """Valid provider config parses correctly."""
        cfg = ProviderConfig(
            name="openai", model="gpt-4", credentials_secret="openai-key"
        )
        assert cfg.name == "openai"
        assert cfg.model == "gpt-4"

    def test_invalid_provider_rejected(self) -> None:
        """Invalid provider name is rejected."""
        with pytest.raises(Exception):
            ProviderConfig(name="invalid", model="x", credentials_secret="x")


class TestStepResult:
    """Tests for StepResult model."""

    def test_completed_result(self) -> None:
        """Completed step has output."""
        r = StepResult(status="completed", output={"summary": "done"})
        assert r.status == "completed"
        assert r.output["summary"] == "done"

    def test_failed_result(self) -> None:
        """Failed step has error."""
        r = StepResult(status="failed", error="timeout")
        assert r.status == "failed"
        assert r.error == "timeout"

    def test_denied_result(self) -> None:
        """Denied step from approval timeout."""
        r = StepResult(status="denied", output={"reason": "timeout"})
        assert r.status == "denied"


class TestWorkflowInput:
    """Tests for WorkflowInput model."""

    def test_minimal_input(self) -> None:
        """Minimal input with required fields."""
        inp = WorkflowInput(
            definition={"steps": []},
            workflow_id="wf-1",
            provider=ProviderConfig(
                name="openai", model="gpt-4", credentials_secret="k"
            ),
        )
        assert inp.workflow_id == "wf-1"
        assert inp.sandbox_image == "lightspeed-agentic-sandbox:latest"
        assert inp.skills_image is None

    def test_full_input(self) -> None:
        """Full input with all optional fields."""
        inp = WorkflowInput(
            definition={"steps": [{"name": "s1"}]},
            input_prompt="check cluster",
            workflow_id="wf-2",
            provider=ProviderConfig(
                name="claude", model="claude-4", credentials_secret="k"
            ),
            sandbox_image="custom:v1",
            skills_image="quay.io/skills:latest",
            skills_paths=["/skills/diag"],
        )
        assert inp.input_prompt == "check cluster"
        assert inp.skills_image == "quay.io/skills:latest"


class TestWorkflowOutput:
    """Tests for WorkflowOutput model."""

    def test_empty_output(self) -> None:
        """Empty output has no steps."""
        out = WorkflowOutput()
        assert out.steps == {}

    def test_output_with_steps(self) -> None:
        """Output with completed steps."""
        out = WorkflowOutput(
            steps={
                "diagnosis": StepResult(status="completed", output={"summary": "ok"}),
            }
        )
        assert out.steps["diagnosis"].status == "completed"


class TestWorkflowStatus:
    """Tests for WorkflowStatus model."""

    def test_status_with_events(self) -> None:
        """Status includes step results and events."""
        status = WorkflowStatus(
            steps={"s1": StepResult(status="completed")},
            events=[
                WorkflowEvent(
                    type="step.completed", step="s1", timestamp="2026-01-01T00:00:00Z"
                )
            ],
        )
        assert len(status.events) == 1
        assert status.events[0].type == "step.completed"


class TestSandboxStepInput:
    """Tests for SandboxStepInput model."""

    def test_minimal_step_input(self) -> None:
        """Minimal sandbox step input."""
        inp = SandboxStepInput(
            step={"name": "diagnose", "type": "agent"},
            workflow_id="wf-1",
            provider=ProviderConfig(
                name="openai", model="gpt-4", credentials_secret="k"
            ),
            sandbox_image="sandbox:latest",
        )
        assert inp.workflow_id == "wf-1"
        assert inp.context == {}


class TestMCPModels:
    """Tests for MCP server injection models."""

    def test_mcp_server_config_basic(self) -> None:
        """MCPServerConfig stores name and URL."""
        from cloud_agents.workflow.temporal_models import MCPServerConfig

        cfg = MCPServerConfig(name="sn", url="http://mcp.local/sse")
        assert cfg.name == "sn"
        assert cfg.url == "http://mcp.local/sse"
        assert cfg.headers is None
        assert cfg.secret_headers is None

    def test_mcp_server_config_with_headers(self) -> None:
        """MCPServerConfig stores plain text headers."""
        from cloud_agents.workflow.temporal_models import MCPServerConfig

        cfg = MCPServerConfig(
            name="sn",
            url="http://mcp.local/sse",
            headers={"X-Custom": "val"},
        )
        assert cfg.headers == {"X-Custom": "val"}

    def test_mcp_server_config_with_secret_headers(self) -> None:
        """MCPServerConfig stores Secret-backed header references."""
        from cloud_agents.workflow.temporal_models import MCPServerConfig, SecretHeaderRef

        cfg = MCPServerConfig(
            name="sn",
            url="http://mcp.local/sse",
            secret_headers={
                "Authorization": SecretHeaderRef(
                    secret_name="mcp-token", key="bearer-token"
                ),
            },
        )
        assert cfg.secret_headers["Authorization"].secret_name == "mcp-token"
        assert cfg.secret_headers["Authorization"].key == "bearer-token"

    def test_secret_header_ref_fields(self) -> None:
        """SecretHeaderRef stores secret_name and key."""
        from cloud_agents.workflow.temporal_models import SecretHeaderRef

        ref = SecretHeaderRef(secret_name="my-secret", key="api-key")
        assert ref.secret_name == "my-secret"
        assert ref.key == "api-key"

    def test_workflow_input_accepts_mcp_servers(self) -> None:
        """WorkflowInput accepts an optional mcp_servers list."""
        from cloud_agents.workflow.temporal_models import MCPServerConfig

        wi = WorkflowInput(
            definition={"spec": {"steps": []}},
            workflow_id="wf-1",
            provider=ProviderConfig(
                name="openai", model="gpt-4", credentials_secret="k"
            ),
            mcp_servers=[
                MCPServerConfig(name="sn", url="http://mcp.local/sse"),
            ],
        )
        assert len(wi.mcp_servers) == 1
        assert wi.mcp_servers[0].name == "sn"

    def test_workflow_input_mcp_servers_defaults_none(self) -> None:
        """WorkflowInput mcp_servers defaults to None."""
        wi = WorkflowInput(
            definition={"spec": {"steps": []}},
            workflow_id="wf-1",
            provider=ProviderConfig(
                name="openai", model="gpt-4", credentials_secret="k"
            ),
        )
        assert wi.mcp_servers is None


class TestProviderConfigModelProvider:
    """Tests for model_provider field on ProviderConfig."""

    def test_accepts_model_provider(self) -> None:
        """ProviderConfig accepts optional model_provider field."""
        cfg = ProviderConfig(
            name="openai",
            model="gpt-4",
            credentials_secret="k",
            model_provider="anthropic",
        )
        assert cfg.model_provider == "anthropic"

    def test_defaults_none(self) -> None:
        """model_provider defaults to None when not provided."""
        cfg = ProviderConfig(name="openai", model="gpt-4", credentials_secret="k")
        assert cfg.model_provider is None
