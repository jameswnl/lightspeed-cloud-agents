"""Tests for startup reconciliation of orphaned sandbox containers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cloud_agents.workflow.executor.temporal.entrypoint import reconcile_orphaned_sandboxes

OWNER_KEY = "cloud-agents/owner-instance"


def _fake_spawner(sandboxes: dict[str, str | None]) -> AsyncMock:
    """Build a spawner whose list_active() honours label selectors.

    sandboxes maps agent name -> owner-instance label value (None = legacy
    sandbox carrying only spawned-by=workflow-runner).
    """
    spawner = AsyncMock()

    async def list_active(labels: dict[str, str] | None = None) -> list[str]:
        labels = labels or {}
        assert labels.get("spawned-by") == "workflow-runner"
        if OWNER_KEY in labels:
            return [name for name, owner in sandboxes.items() if owner == labels[OWNER_KEY]]
        return list(sandboxes)

    spawner.list_active.side_effect = list_active
    return spawner


class TestStartupReconciliation:
    """Tests for reconcile_orphaned_sandboxes."""

    @pytest.mark.asyncio
    async def test_orphaned_containers_destroyed_on_startup(self) -> None:
        """Startup reconciliation destroys this instance's orphaned sandboxes."""
        mock_spawner = _fake_spawner({"ca-orphan1": "runner-a", "ca-orphan2": "runner-a"})

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a")

        mock_spawner.list_active.assert_any_call(
            {"spawned-by": "workflow-runner", OWNER_KEY: "runner-a"},
        )
        assert mock_spawner.destroy.call_count == 2
        mock_spawner.destroy.assert_any_call("ca-orphan1")
        mock_spawner.destroy.assert_any_call("ca-orphan2")

    @pytest.mark.asyncio
    async def test_other_instance_sandboxes_survive(self) -> None:
        """A restarting replica must not destroy another live replica's sandboxes."""
        mock_spawner = _fake_spawner(
            {"ca-mine": "runner-a", "ca-theirs1": "runner-b", "ca-theirs2": "runner-c"}
        )

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a")

        mock_spawner.destroy.assert_called_once_with("ca-mine")

    @pytest.mark.asyncio
    async def test_legacy_unowned_sandboxes_left_alone(self) -> None:
        """Sandboxes with spawned-by but no owner-instance label are never destroyed.

        They predate owner labelling, so there is no way to tell whether a
        live replica still uses them (e.g. a long-lived chat sandbox).
        """
        mock_spawner = _fake_spawner({"ca-legacy": None, "ca-mine": "runner-a"})

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a")

        mock_spawner.destroy.assert_called_once_with("ca-mine")

    @pytest.mark.asyncio
    async def test_unset_identity_destroys_nothing(self, caplog) -> None:
        """Without an instance identity, reconcile fails safe: no destroys, a warning."""
        mock_spawner = _fake_spawner({"ca-legacy": None, "ca-other": "runner-b"})

        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="")

        mock_spawner.destroy.assert_not_called()
        assert "RUNNER_INSTANCE_ID" in caplog.text

    @pytest.mark.asyncio
    async def test_unset_identity_is_default(self) -> None:
        """Callers that pass no identity get the fail-safe behaviour."""
        mock_spawner = _fake_spawner({"ca-any": None})

        await reconcile_orphaned_sandboxes(mock_spawner)

        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_orphans_no_destroy(self) -> None:
        """Startup with no orphans calls no destroy."""
        mock_spawner = _fake_spawner({})

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a")

        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_destroy_failure_logged_not_raised(self) -> None:
        """If destroy fails for one orphan, others are still attempted."""
        mock_spawner = _fake_spawner({"orphan1": "a", "orphan2": "a", "orphan3": "a"})
        mock_spawner.destroy.side_effect = [
            None,
            RuntimeError("destroy failed"),
            None,
        ]

        # Should not raise
        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="a")

        assert mock_spawner.destroy.call_count == 3

    @pytest.mark.asyncio
    async def test_none_spawner_is_noop(self) -> None:
        """reconcile_orphaned_sandboxes with None spawner does nothing."""
        # Should not raise
        await reconcile_orphaned_sandboxes(None, owner_instance="runner-a")


class TestCreateSpawnerOwnerInstance:
    """_create_spawner() threads RUNNER_INSTANCE_ID into the spawner."""

    def test_runner_instance_id_passed_to_spawner(self, mocker) -> None:
        from cloud_agents.workflow.executor.temporal import entrypoint

        mocker.patch.object(entrypoint, "SPAWNER_TYPE", "openshell")
        mocker.patch.object(entrypoint, "RUNNER_INSTANCE_ID", "runner-a", create=True)
        build = mocker.patch("cloud_agents.spawner.factory.build_spawner")

        entrypoint._create_spawner()

        assert build.call_args.kwargs["owner_instance"] == "runner-a"
