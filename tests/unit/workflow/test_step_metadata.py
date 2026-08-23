"""Tests for StepMetadata and ConversationMessage dataclasses."""

from __future__ import annotations

from datetime import datetime


class TestStepMetadata:
    """Tests for StepMetadata typed dataclass."""

    def test_import(self) -> None:
        """StepMetadata can be imported from step.base."""
        from cloud_agents.workflow.executor.step.base import StepMetadata

        assert StepMetadata is not None

    def test_default_construction(self) -> None:
        """StepMetadata can be constructed with all defaults."""
        from cloud_agents.workflow.executor.step.base import StepMetadata

        meta = StepMetadata()
        assert meta.user_id is None
        assert meta.session_id is None
        assert meta.trace_id is None
        assert meta.conversation_id is None
        assert meta.extra == {}

    def test_construction_with_values(self) -> None:
        """StepMetadata accepts identity fields."""
        from cloud_agents.workflow.executor.step.base import StepMetadata

        meta = StepMetadata(
            user_id="user-42",
            session_id="sess-abc",
            trace_id="trace-xyz",
            conversation_id="conv-123",
            extra={"custom_key": "custom_value"},
        )
        assert meta.user_id == "user-42"
        assert meta.session_id == "sess-abc"
        assert meta.trace_id == "trace-xyz"
        assert meta.conversation_id == "conv-123"
        assert meta.extra == {"custom_key": "custom_value"}

    def test_extra_defaults_to_empty_dict(self) -> None:
        """Each StepMetadata instance gets its own empty dict for extra."""
        from cloud_agents.workflow.executor.step.base import StepMetadata

        m1 = StepMetadata()
        m2 = StepMetadata()
        m1.extra["key"] = "value"
        assert m2.extra == {}

    def test_step_input_accepts_metadata(self) -> None:
        """StepInput accepts StepMetadata in metadata field."""
        from cloud_agents.workflow.executor.step.base import StepInput, StepMetadata

        meta = StepMetadata(user_id="user-1")
        step_input = StepInput(
            prompt="test prompt",
            provider={"name": "openai", "model": "gpt-4o"},
            metadata=meta,
        )
        assert step_input.metadata is not None
        assert step_input.metadata.user_id == "user-1"

    def test_step_input_metadata_default_is_none(self) -> None:
        """StepInput metadata defaults to None for backward compatibility."""
        from cloud_agents.workflow.executor.step.base import StepInput

        step_input = StepInput(
            prompt="test prompt",
            provider={"name": "openai"},
        )
        assert step_input.metadata is None


class TestConversationMessage:
    """Tests for ConversationMessage dataclass."""

    def test_import(self) -> None:
        """ConversationMessage can be imported from step.conversation."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        assert ConversationMessage is not None

    def test_construction_with_required_fields(self) -> None:
        """ConversationMessage requires role and content."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        msg = ConversationMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert isinstance(msg.timestamp, datetime)
        assert msg.metadata == {}

    def test_all_roles(self) -> None:
        """ConversationMessage supports all defined roles."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        for role in ("user", "assistant", "tool_call", "tool_result"):
            msg = ConversationMessage(role=role, content="test")
            assert msg.role == role

    def test_custom_timestamp(self) -> None:
        """ConversationMessage accepts a custom timestamp."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        ts = datetime(2026, 1, 1, 12, 0, 0)
        msg = ConversationMessage(role="user", content="hello", timestamp=ts)
        assert msg.timestamp == ts

    def test_metadata_isolation(self) -> None:
        """Each ConversationMessage gets its own metadata dict."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        m1 = ConversationMessage(role="user", content="a")
        m2 = ConversationMessage(role="user", content="b")
        m1.metadata["key"] = "value"
        assert m2.metadata == {}

    def test_tool_call_metadata(self) -> None:
        """ConversationMessage with tool_call role carries tool metadata."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        msg = ConversationMessage(
            role="tool_call",
            content="kubectl get pods",
            metadata={"tool_name": "kubectl", "tool_args": {"namespace": "default"}},
        )
        assert msg.metadata["tool_name"] == "kubectl"

    def test_to_dict(self) -> None:
        """ConversationMessage can be serialized to dict."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        ts = datetime(2026, 1, 1, 12, 0, 0)
        msg = ConversationMessage(role="user", content="hello", timestamp=ts)
        d = msg.to_dict()
        assert d["role"] == "user"
        assert d["content"] == "hello"
        assert d["timestamp"] == ts.isoformat()
        assert d["metadata"] == {}

    def test_from_dict(self) -> None:
        """ConversationMessage can be deserialized from dict."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        data = {
            "role": "assistant",
            "content": "I found the issue",
            "timestamp": "2026-01-01T12:00:00",
            "metadata": {"model": "gpt-4o"},
        }
        msg = ConversationMessage.from_dict(data)
        assert msg.role == "assistant"
        assert msg.content == "I found the issue"
        assert msg.metadata["model"] == "gpt-4o"

    def test_from_dict_without_optional_fields(self) -> None:
        """ConversationMessage.from_dict works with minimal fields."""
        from cloud_agents.workflow.executor.step.conversation import ConversationMessage

        data = {"role": "user", "content": "hello"}
        msg = ConversationMessage.from_dict(data)
        assert msg.role == "user"
        assert msg.content == "hello"
