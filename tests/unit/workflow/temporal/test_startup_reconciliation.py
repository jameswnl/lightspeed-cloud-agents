"""Tests for startup reconciliation of orphaned sandbox containers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cloud_agents.workflow.temporal_entrypoint import reconcile_orphaned_sandboxes


class TestStartupReconciliation:
    """Tests for reconcile_orphaned_sandboxes."""

    @pytest.mark.asyncio
    async def test_orphaned_containers_destroyed_on_startup(self) -> None:
        """Startup reconciliation destroys orphaned sandbox containers."""
        mock_spawner = AsyncMock()
        mock_spawner.list_active.return_value = ["ca-orphan1", "ca-orphan2"]

        await reconcile_orphaned_sandboxes(mock_spawner)

        mock_spawner.list_active.assert_called_once_with(
            {"spawned-by": "workflow-runner"},
        )
        assert mock_spawner.destroy.call_count == 2
        mock_spawner.destroy.assert_any_call("ca-orphan1")
        mock_spawner.destroy.assert_any_call("ca-orphan2")

    @pytest.mark.asyncio
    async def test_no_orphans_no_destroy(self) -> None:
        """Startup with no orphans calls no destroy."""
        mock_spawner = AsyncMock()
        mock_spawner.list_active.return_value = []

        await reconcile_orphaned_sandboxes(mock_spawner)

        mock_spawner.list_active.assert_called_once_with(
            {"spawned-by": "workflow-runner"},
        )
        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_destroy_failure_logged_not_raised(self) -> None:
        """If destroy fails for one orphan, others are still attempted."""
        mock_spawner = AsyncMock()
        mock_spawner.list_active.return_value = ["orphan1", "orphan2", "orphan3"]
        mock_spawner.destroy.side_effect = [
            None,
            RuntimeError("destroy failed"),
            None,
        ]

        # Should not raise
        await reconcile_orphaned_sandboxes(mock_spawner)

        assert mock_spawner.destroy.call_count == 3

    @pytest.mark.asyncio
    async def test_none_spawner_is_noop(self) -> None:
        """reconcile_orphaned_sandboxes with None spawner does nothing."""
        # Should not raise
        await reconcile_orphaned_sandboxes(None)
