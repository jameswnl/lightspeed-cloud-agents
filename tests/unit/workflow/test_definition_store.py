"""Unit tests for workflow definition store."""

from __future__ import annotations

import pytest

from agents.workflow.definition import (
    WorkflowDefinition,
    WorkflowSpec,
    WorkflowStepSpec,
)
from agents.workflow.definition_store import DefinitionStore


def _make_defn(name: str = "test-wf") -> WorkflowDefinition:
    """Create a test workflow definition."""
    return WorkflowDefinition(
        apiVersion="v1",
        kind="AgentWorkflow",
        metadata={"name": name},
        spec=WorkflowSpec(
            steps=[
                WorkflowStepSpec(
                    name="s1",
                    type="agent",
                    agent="diag",
                    prompt="test",
                    output_key="r1",
                    spawn="pre-deployed",
                ),
            ]
        ),
    )


class TestDefinitionStore:
    """Tests for DefinitionStore."""

    @pytest.mark.asyncio
    async def test_save_and_get(self) -> None:
        """Test saving and retrieving a definition."""
        store = DefinitionStore()
        stored = await store.save(_make_defn("my-wf"))
        assert stored.name == "my-wf"
        assert stored.version == 1

        retrieved = await store.get("my-wf")
        assert retrieved is not None
        assert retrieved.version == 1

    @pytest.mark.asyncio
    async def test_versioning(self) -> None:
        """Test that each save creates a new version."""
        store = DefinitionStore()
        v1 = await store.save(_make_defn("wf"))
        v2 = await store.save(_make_defn("wf"))
        assert v1.version == 1
        assert v2.version == 2

        latest = await store.get("wf")
        assert latest.version == 2

        old = await store.get_version("wf", 1)
        assert old.version == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent(self) -> None:
        """Test getting a nonexistent definition returns None."""
        store = DefinitionStore()
        assert await store.get("missing") is None

    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        """Test listing all active definitions."""
        store = DefinitionStore()
        await store.save(_make_defn("wf-1"))
        await store.save(_make_defn("wf-2"))
        defs = await store.list_all()
        assert len(defs) == 2

    @pytest.mark.asyncio
    async def test_soft_delete(self) -> None:
        """Test soft-deleting a definition."""
        store = DefinitionStore()
        await store.save(_make_defn("wf"))
        assert await store.delete("wf") is True
        assert await store.get("wf") is None

        defs = await store.list_all()
        assert len(defs) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self) -> None:
        """Test deleting a nonexistent definition returns False."""
        store = DefinitionStore()
        assert await store.delete("missing") is False
