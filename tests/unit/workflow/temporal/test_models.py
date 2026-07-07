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


class TestTranscriptEvent:
    """Tests for TranscriptEvent model."""

    def test_tool_call_event(self) -> None:
        """Tool call event captures name, input, output, duration."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(
            ts="2026-01-01T00:00:00Z",
            type="tool_call",
            data={"name": "kubectl", "input": "get pods", "output": "pod-1", "duration_ms": 150},
        )
        assert event.type == "tool_call"
        assert event.data["name"] == "kubectl"
        assert event.data["duration_ms"] == 150

    def test_thinking_event(self) -> None:
        """Thinking event captures reasoning text."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(
            ts="2026-01-01T00:00:01Z",
            type="thinking",
            data={"text": "analyzing logs..."},
        )
        assert event.type == "thinking"

    def test_result_event(self) -> None:
        """Result event captures final output."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(
            ts="2026-01-01T00:00:02Z",
            type="result",
            data={"output": {"summary": "done"}},
        )
        assert event.type == "result"

    def test_error_event(self) -> None:
        """Error event captures error details."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(
            ts="2026-01-01T00:00:03Z",
            type="error",
            data={"message": "API timeout", "stack": "traceback..."},
        )
        assert event.type == "error"

    def test_tool_result_event(self) -> None:
        """Tool result event is a valid type."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(
            ts="2026-01-01T00:00:04Z",
            type="tool_result",
            data={"name": "kubectl", "output": "pod-1 Running"},
        )
        assert event.type == "tool_result"

    def test_default_data(self) -> None:
        """TranscriptEvent defaults data to empty dict when omitted."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(ts="2026-01-01T00:00:00Z", type="result")
        assert event.data == {}

    def test_invalid_type_rejected(self) -> None:
        """Invalid event type is rejected."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        with pytest.raises(Exception):
            TranscriptEvent(
                ts="2026-01-01T00:00:00Z",
                type="invalid_type",
                data={},
            )

    def test_serialization_roundtrip(self) -> None:
        """TranscriptEvent serializes and deserializes cleanly."""
        from cloud_agents.workflow.temporal_models import TranscriptEvent

        event = TranscriptEvent(
            ts="2026-01-01T00:00:00Z",
            type="tool_call",
            data={"name": "kubectl"},
        )
        dumped = event.model_dump()
        restored = TranscriptEvent(**dumped)
        assert restored.ts == event.ts
        assert restored.type == event.type
        assert restored.data == event.data


class TestStepTranscript:
    """Tests for StepTranscript model."""

    def test_empty_transcript(self) -> None:
        """Empty transcript has no events and zero counters."""
        from cloud_agents.workflow.temporal_models import StepTranscript

        transcript = StepTranscript(step_name="diagnose")
        assert transcript.step_name == "diagnose"
        assert transcript.events == []
        assert transcript.cost_usd is None
        assert transcript.input_tokens is None
        assert transcript.output_tokens is None
        assert transcript.duration_ms is None

    def test_full_transcript(self) -> None:
        """Full transcript with events and metrics."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        events = [
            TranscriptEvent(
                ts="2026-01-01T00:00:00Z",
                type="tool_call",
                data={"name": "kubectl", "duration_ms": 100},
            ),
            TranscriptEvent(
                ts="2026-01-01T00:00:01Z",
                type="result",
                data={"output": "done"},
            ),
        ]
        transcript = StepTranscript(
            step_name="diagnose",
            events=events,
            cost_usd=0.05,
            input_tokens=1000,
            output_tokens=500,
            duration_ms=1500,
        )
        assert len(transcript.events) == 2
        assert transcript.cost_usd == 0.05
        assert transcript.input_tokens == 1000

    def test_serialization_roundtrip(self) -> None:
        """StepTranscript serializes and deserializes for Temporal memo."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        transcript = StepTranscript(
            step_name="fix",
            events=[
                TranscriptEvent(
                    ts="2026-01-01T00:00:00Z",
                    type="tool_call",
                    data={"name": "kubectl"},
                ),
            ],
            cost_usd=0.01,
            duration_ms=500,
        )
        dumped = transcript.model_dump()
        restored = StepTranscript(**dumped)
        assert restored.step_name == transcript.step_name
        assert len(restored.events) == 1
        assert restored.cost_usd == transcript.cost_usd

    def test_full_transcript_roundtrip(self) -> None:
        """Full transcript with non-None tokens survives dump/restore."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        transcript = StepTranscript(
            step_name="fix",
            events=[
                TranscriptEvent(ts="t", type="tool_call", data={"name": "kubectl"}),
            ],
            cost_usd=0.05,
            input_tokens=1000,
            output_tokens=500,
            duration_ms=1500,
        )
        dumped = transcript.model_dump()
        restored = StepTranscript(**dumped)
        assert restored.input_tokens == 1000
        assert restored.output_tokens == 500
        assert restored.cost_usd == 0.05

    def test_truncate_large_transcript(self) -> None:
        """truncate() keeps first/last events plus summary counts."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        events = [
            TranscriptEvent(
                ts=f"2026-01-01T00:00:{i:02d}Z",
                type="tool_call",
                data={"name": f"tool_{i}", "input": "x" * 500, "output": "y" * 500},
            )
            for i in range(120)
        ]
        transcript = StepTranscript(step_name="big", events=events, duration_ms=60000)
        truncated = transcript.truncate(max_events=20)
        # 10 first + 1 marker + 10 last = 21
        assert len(truncated.events) == 21
        assert truncated.step_name == "big"
        assert truncated.duration_ms == 60000

    def test_truncate_max_events_1(self) -> None:
        """truncate() with max_events=1 does not crash (half=0 edge case)."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        events = [
            TranscriptEvent(ts=f"t{i}", type="tool_call", data={"name": f"tool_{i}"})
            for i in range(10)
        ]
        transcript = StepTranscript(step_name="edge", events=events)
        truncated = transcript.truncate(max_events=1)
        # half=0 means empty first/last + marker = 1 event
        assert len(truncated.events) == 1
        assert truncated.events[0].data.get("_truncated") is True

    def test_truncate_small_transcript_unchanged(self) -> None:
        """truncate() on a small transcript returns it unchanged."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        events = [
            TranscriptEvent(
                ts="2026-01-01T00:00:00Z",
                type="tool_call",
                data={"name": "kubectl"},
            ),
        ]
        transcript = StepTranscript(step_name="small", events=events)
        truncated = transcript.truncate(max_events=20)
        assert len(truncated.events) == 1

    def test_truncate_preserves_tool_names_drops_payloads(self) -> None:
        """Smart truncation keeps tool names/durations but drops large payloads."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

        events = [
            TranscriptEvent(
                ts="2026-01-01T00:00:00Z",
                type="tool_call",
                data={
                    "name": "kubectl",
                    "input": "x" * 10000,
                    "output": "y" * 10000,
                    "duration_ms": 150,
                },
            ),
        ]
        transcript = StepTranscript(step_name="test", events=events)
        truncated = transcript.truncate(max_events=20, max_payload_bytes=256)
        event_data = truncated.events[0].data
        assert event_data["name"] == "kubectl"
        assert event_data["duration_ms"] == 150
        # Large payloads should be truncated with suffix
        assert event_data["input"].endswith("...(truncated)")
        assert event_data["output"].endswith("...(truncated)")
        # Truncated length should be max_payload_bytes + len("...(truncated)")
        assert len(event_data["input"]) == 256 + len("...(truncated)")


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
