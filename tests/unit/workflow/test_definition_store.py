"""Unit tests for workflow definition store."""

from __future__ import annotations

import pytest

from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.definition_store import DefinitionStore


def _make_defn(name: str = "test-wf") -> WorkflowDefinition:
    """Create a test workflow definition."""
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": name},
        spec=WorkflowSpec(steps=[
            WorkflowStepSpec(name="s1", type="agent", agent="diag",
                             prompt="test", output_key="r1", spawn="pre-deployed"),
        ]),
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


class TestSharedDefinitionStore:
    """Tests for cross-instance shared persistence."""

    @pytest.mark.asyncio
    async def test_definition_visible_across_instances(self) -> None:
        """Test that a definition saved by one store is readable by another."""
        from agents.workflow.persistence import InMemoryPersistence
        shared = InMemoryPersistence()

        store_a = DefinitionStore(persistence=shared)
        store_b = DefinitionStore(persistence=shared)

        await store_a.save(_make_defn("shared-wf"))

        retrieved = await store_b.get("shared-wf")
        assert retrieved is not None
        assert retrieved.name == "shared-wf"

    @pytest.mark.asyncio
    async def test_list_all_across_instances(self) -> None:
        """Test that list_all returns definitions from shared persistence."""
        from agents.workflow.persistence import InMemoryPersistence
        shared = InMemoryPersistence()

        store_a = DefinitionStore(persistence=shared)
        store_b = DefinitionStore(persistence=shared)

        await store_a.save(_make_defn("wf-1"))
        await store_a.save(_make_defn("wf-2"))

        defs = await store_b.list_all()
        names = {d.name for d in defs}
        assert "wf-1" in names
        assert "wf-2" in names

    @pytest.mark.asyncio
    async def test_version_allocation_across_instances(self) -> None:
        """Test that versions increment correctly across store instances."""
        from agents.workflow.persistence import InMemoryPersistence
        shared = InMemoryPersistence()

        store_a = DefinitionStore(persistence=shared)
        store_b = DefinitionStore(persistence=shared)

        v1 = await store_a.save(_make_defn("ver-wf"))
        assert v1.version == 1

        v2 = await store_b.save(_make_defn("ver-wf"))
        assert v2.version == 2

    @pytest.mark.asyncio
    async def test_get_version_across_instances(self) -> None:
        """Test that get_version reads from shared persistence."""
        from agents.workflow.persistence import InMemoryPersistence
        shared = InMemoryPersistence()

        store_a = DefinitionStore(persistence=shared)
        store_b = DefinitionStore(persistence=shared)

        await store_a.save(_make_defn("vget-wf"))
        await store_a.save(_make_defn("vget-wf"))

        v1 = await store_b.get_version("vget-wf", 1)
        assert v1 is not None
        assert v1.version == 1

        v2 = await store_b.get_version("vget-wf", 2)
        assert v2 is not None
        assert v2.version == 2

    @pytest.mark.asyncio
    async def test_delete_across_instances(self) -> None:
        """Test that deleting on one store is visible on another."""
        from agents.workflow.persistence import InMemoryPersistence
        shared = InMemoryPersistence()

        store_a = DefinitionStore(persistence=shared)
        store_b = DefinitionStore(persistence=shared)

        await store_a.save(_make_defn("del-wf"))
        assert await store_b.get("del-wf") is not None

        await store_a.delete("del-wf")
        result = await store_b.get("del-wf")
        assert result is None or not result.active


class TestStatelessExecutor:
    """Tests that the executor uses persistence instead of in-memory state."""

    @pytest.mark.asyncio
    async def test_get_state_uses_persistence(self) -> None:
        """Test that get_state queries persistence, not in-memory."""
        from unittest.mock import AsyncMock
        from agents.models import AgentRunResponse
        from agents.registry import AgentRegistry
        from agents.workflow.executor import WorkflowExecutor

        defn = _make_defn()
        client = AsyncMock()
        client.run = AsyncMock(return_value=AgentRunResponse(
            output={"summary": "ok"}, output_type="str",
            usage={"input_tokens": 1, "output_tokens": 1},
            agent_name="diag", success=True,
        ))
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)

        state = await executor.run()
        assert state.status == "completed"

        retrieved = await executor.get_state(state.workflow_id)
        assert retrieved is not None
        assert retrieved.workflow_id == state.workflow_id

    @pytest.mark.asyncio
    async def test_list_workflows_uses_persistence(self) -> None:
        """Test that list_workflows queries persistence."""
        from unittest.mock import AsyncMock
        from agents.models import AgentRunResponse
        from agents.registry import AgentRegistry
        from agents.workflow.executor import WorkflowExecutor

        defn = _make_defn()
        client = AsyncMock()
        client.run = AsyncMock(return_value=AgentRunResponse(
            output={"summary": "ok"}, output_type="str",
            usage={"input_tokens": 1, "output_tokens": 1},
            agent_name="diag", success=True,
        ))
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)

        await executor.run()
        workflows = await executor.list_workflows()
        assert len(workflows) == 1

    @pytest.mark.asyncio
    async def test_paused_step_index_persisted(self) -> None:
        """Test that paused_step_index is stored in WorkflowState."""
        from unittest.mock import AsyncMock
        from agents.models import AgentRunResponse
        from agents.registry import AgentRegistry
        from agents.workflow.executor import WorkflowExecutor

        defn = WorkflowDefinition(
            apiVersion="v1", kind="AgentWorkflow",
            metadata={"name": "test"},
            spec=WorkflowSpec(steps=[
                WorkflowStepSpec(name="s1", type="agent", agent="diag",
                                 prompt="test", output_key="r1", spawn="pre-deployed"),
                WorkflowStepSpec(name="approve", type="human-approval",
                                 message="OK?", output_key="a"),
            ]),
        )
        client = AsyncMock()
        client.run = AsyncMock(return_value=AgentRunResponse(
            output={"summary": "ok"}, output_type="str",
            usage={"input_tokens": 1, "output_tokens": 1},
            agent_name="diag", success=True,
        ))
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)

        state = await executor.run()
        assert state.status == "paused"
        assert state.paused_step_index == 1

        retrieved = await executor.get_state(state.workflow_id)
        assert retrieved.paused_step_index == 1
