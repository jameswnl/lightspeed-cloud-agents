"""Integration tests: identity model flows through the full execution pipeline.

Proves that StepMetadata, ConversationMessage serialization, and identity
wiring work end-to-end — not just with mocked unit tests.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


class TestStepMetadataFlowsThroughGraph:
    """Verify StepMetadata is wired from graph_translator to executor."""

    @pytest.mark.asyncio
    async def test_metadata_reaches_executor_with_user_id(
        self, mocker: MockerFixture
    ) -> None:
        """StepMetadata with user_id flows from build_graph to executor.run()."""
        from cloud_agents.workflow.executor.step.base import StepMetadata

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = mocker.MagicMock(
            status="completed",
            output={"ok": True},
            error=None,
            transcript=[],
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "identity-test"},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "test",
                        "output_key": "r1",
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-identity-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        # Set identity on state (caller would do this before running)
        state.user_id = "jwong"
        state.session_id = "ses-abc"

        await graph.run(state=state)

        call_args = mock_executor.run.call_args[0][0]
        assert call_args.metadata is not None
        assert isinstance(call_args.metadata, StepMetadata)
        assert call_args.metadata.user_id == "jwong"
        assert call_args.metadata.session_id == "ses-abc"

    @pytest.mark.asyncio
    async def test_metadata_none_when_no_identity(
        self, mocker: MockerFixture
    ) -> None:
        """StepMetadata has None fields when no identity is provided."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = mocker.MagicMock(
            status="completed",
            output={"ok": True},
            error=None,
            transcript=[],
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "no-identity"},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "spawn": "none",
                        "prompt": "test",
                        "output_key": "r1",
                    },
                ],
            },
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-no-identity",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        call_args = mock_executor.run.call_args[0][0]
        assert call_args.metadata is not None
        assert call_args.metadata.user_id is None
        assert call_args.metadata.session_id is None


class TestConversationMessageRoundTrip:
    """Verify ConversationMessage serializes/deserializes correctly for storage."""

    def test_single_message_round_trip(self) -> None:
        """Single ConversationMessage round-trips through JSON."""
        from cloud_agents.workflow.executor.step.conversation import (
            ConversationMessage,
        )

        msg = ConversationMessage(
            role="user",
            content="What pods are running?",
            timestamp=datetime(2026, 8, 23, 12, 0, 0),
        )

        serialized = json.dumps(msg.to_dict())
        restored = ConversationMessage.from_dict(json.loads(serialized))

        assert restored.role == "user"
        assert restored.content == "What pods are running?"
        assert restored.timestamp.year == 2026

    def test_conversation_history_round_trip(self) -> None:
        """Full multi-turn conversation round-trips through JSON."""
        from cloud_agents.workflow.executor.step.conversation import (
            ConversationMessage,
        )

        history = [
            ConversationMessage(
                role="user",
                content="What pods are running?",
                timestamp=datetime(2026, 8, 23, 12, 0, 0),
            ),
            ConversationMessage(
                role="assistant",
                content="There are 8 pods in kube-system.",
                timestamp=datetime(2026, 8, 23, 12, 0, 1),
            ),
            ConversationMessage(
                role="tool_call",
                content="",
                timestamp=datetime(2026, 8, 23, 12, 0, 2),
                metadata={"tool_name": "kubectl_get", "args": {"resource": "pods"}},
            ),
            ConversationMessage(
                role="tool_result",
                content='{"items": []}',
                timestamp=datetime(2026, 8, 23, 12, 0, 3),
                metadata={"tool_name": "kubectl_get"},
            ),
        ]

        serialized = json.dumps([m.to_dict() for m in history])
        restored = [
            ConversationMessage.from_dict(d) for d in json.loads(serialized)
        ]

        assert len(restored) == 4
        assert restored[0].role == "user"
        assert restored[1].role == "assistant"
        assert restored[2].role == "tool_call"
        assert restored[2].metadata["tool_name"] == "kubectl_get"
        assert restored[3].role == "tool_result"

    def test_tool_call_metadata_preserved(self) -> None:
        """Tool call args and name survive serialization."""
        from cloud_agents.workflow.executor.step.conversation import (
            ConversationMessage,
        )

        msg = ConversationMessage(
            role="tool_call",
            content="",
            metadata={
                "tool_name": "http_request",
                "args": {"url": "https://api.example.com", "method": "GET"},
            },
        )

        restored = ConversationMessage.from_dict(json.loads(json.dumps(msg.to_dict())))
        assert restored.metadata["tool_name"] == "http_request"
        assert restored.metadata["args"]["url"] == "https://api.example.com"


class TestAlembicMigrationFiles:
    """Verify Alembic migration files are valid and consistent."""

    def test_baseline_migration_exists(self) -> None:
        """Baseline migration file exists and has upgrade/downgrade."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "baseline",
            "src/cloud_agents/_alembic/alembic/versions/001_baseline.py",
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "upgrade")
        assert hasattr(mod, "downgrade")

    def test_identity_migration_exists(self) -> None:
        """Identity migration file exists and has upgrade/downgrade."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "identity",
            "src/cloud_agents/_alembic/alembic/versions/002_identity_model.py",
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "upgrade")
        assert hasattr(mod, "downgrade")

    def test_migration_002_includes_identity_columns(self) -> None:
        """Migration 002 adds identity columns to workflow_run_state."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "identity", "src/cloud_agents/_alembic/alembic/versions/002_identity_model.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        source = open("src/cloud_agents/_alembic/alembic/versions/002_identity_model.py").read()
        assert "user_id" in source
        assert "session_id" in source
        assert "parent_workflow_id" in source
        assert "trace_id" in source
        assert "messages" in source
