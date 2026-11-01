"""Tests for startup reconciliation of orphaned sandbox containers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cloud_agents.workflow.executor.temporal import entrypoint
from cloud_agents.workflow.executor.temporal.entrypoint import reconcile_orphaned_sandboxes

OWNER_KEY = "cloud-agents/owner-instance"
EXPIRES_KEY = "cloud-agents/expires-at"
NOW = 1_700_000_000.0
PAST = str(int(NOW) - 1)
FUTURE = str(int(NOW) + 3600)


def _fake_spawner(sandboxes: dict[str, dict[str, str]]) -> AsyncMock:
    """Build a spawner whose list_active_labels() returns sandboxes as-is.

    sandboxes maps agent name -> labels beyond spawned-by (an empty dict is a
    legacy sandbox carrying only spawned-by=workflow-runner).
    """
    spawner = AsyncMock()

    async def list_active_labels(labels: dict[str, str] | None = None):
        assert labels == {"spawned-by": "workflow-runner"}
        return [
            (name, {"spawned-by": "workflow-runner", **extra}) for name, extra in sandboxes.items()
        ]

    spawner.list_active_labels.side_effect = list_active_labels
    return spawner


def _destroyed(spawner: AsyncMock) -> set[str]:
    return {c.args[0] for c in spawner.destroy.call_args_list}


class TestStartupReconciliation:
    """Tests for reconcile_orphaned_sandboxes."""

    @pytest.mark.asyncio
    async def test_orphaned_containers_destroyed_on_startup(self) -> None:
        """Startup reconciliation destroys this instance's orphaned sandboxes,
        expired or not, with a single gateway listing."""
        mock_spawner = _fake_spawner(
            {
                "ca-orphan1": {OWNER_KEY: "runner-a", EXPIRES_KEY: FUTURE},
                "ca-orphan2": {OWNER_KEY: "runner-a"},
            }
        )

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a", now=NOW)

        mock_spawner.list_active_labels.assert_called_once()
        mock_spawner.list_active.assert_not_called()
        assert _destroyed(mock_spawner) == {"ca-orphan1", "ca-orphan2"}

    @pytest.mark.asyncio
    async def test_other_instance_unexpired_sandboxes_survive(self) -> None:
        """A restarting replica must not destroy another live replica's sandboxes."""
        mock_spawner = _fake_spawner(
            {
                "ca-mine": {OWNER_KEY: "runner-a", EXPIRES_KEY: FUTURE},
                "ca-theirs": {OWNER_KEY: "runner-b", EXPIRES_KEY: FUTURE},
            }
        )

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a", now=NOW)

        assert _destroyed(mock_spawner) == {"ca-mine"}

    @pytest.mark.asyncio
    async def test_other_instance_expired_sandbox_destroyed(self) -> None:
        """Sandboxes past expires-at are reaped regardless of owner (e.g. left
        by a replaced pod whose name will never come back)."""
        mock_spawner = _fake_spawner({"ca-stale": {OWNER_KEY: "runner-gone", EXPIRES_KEY: PAST}})

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a", now=NOW)

        assert _destroyed(mock_spawner) == {"ca-stale"}

    @pytest.mark.asyncio
    async def test_malformed_expires_at_kept_and_logged(self, caplog) -> None:
        mock_spawner = _fake_spawner({"ca-weird": {OWNER_KEY: "runner-b", EXPIRES_KEY: "soon"}})

        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a", now=NOW)

        mock_spawner.destroy.assert_not_called()
        assert "ca-weird" in caplog.text

    @pytest.mark.asyncio
    async def test_legacy_sandboxes_left_alone_and_logged(self, caplog) -> None:
        """Sandboxes without expires-at (legacy, or another owner without a
        configured lifetime) are never destroyed by another instance."""
        mock_spawner = _fake_spawner({"ca-legacy": {}, "ca-noexpiry": {OWNER_KEY: "runner-b"}})

        with caplog.at_level("INFO"):
            await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a", now=NOW)

        mock_spawner.destroy.assert_not_called()
        assert "2 sandbox(es)" in caplog.text

    @pytest.mark.asyncio
    async def test_unset_identity_still_reaps_expired(self, caplog) -> None:
        """Without an identity, own-instance cleanup is skipped with a warning,
        but expired sandboxes are still destroyed."""
        mock_spawner = _fake_spawner(
            {
                "ca-expired": {OWNER_KEY: "runner-b", EXPIRES_KEY: PAST},
                "ca-live": {OWNER_KEY: "runner-b", EXPIRES_KEY: FUTURE},
                "ca-legacy": {},
            }
        )

        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="", now=NOW)

        assert _destroyed(mock_spawner) == {"ca-expired"}
        assert "RUNNER_INSTANCE_ID" in caplog.text

    @pytest.mark.asyncio
    async def test_unset_identity_is_default(self) -> None:
        mock_spawner = _fake_spawner({"ca-any": {OWNER_KEY: ""}})

        await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_orphans_no_destroy(self) -> None:
        mock_spawner = _fake_spawner({})

        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="runner-a", now=NOW)

        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_destroy_failure_logged_not_raised(self) -> None:
        """If destroy fails for one orphan, others are still attempted."""
        mock_spawner = _fake_spawner({n: {OWNER_KEY: "a"} for n in ("o1", "o2", "o3")})
        mock_spawner.destroy.side_effect = [None, RuntimeError("destroy failed"), None]

        # Should not raise
        await reconcile_orphaned_sandboxes(mock_spawner, owner_instance="a", now=NOW)

        assert mock_spawner.destroy.call_count == 3

    @pytest.mark.asyncio
    async def test_none_spawner_is_noop(self) -> None:
        """reconcile_orphaned_sandboxes with None spawner does nothing."""
        # Should not raise
        await reconcile_orphaned_sandboxes(None, owner_instance="runner-a")


class TestRunnerInstanceId:
    """_runner_instance_id(): RUNNER_INSTANCE_ID, else HOSTNAME, else ""."""

    def test_explicit_env_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("RUNNER_INSTANCE_ID", "workflow-runner-7d9f-abc12")
        monkeypatch.setenv("HOSTNAME", "other")
        assert entrypoint._runner_instance_id() == "workflow-runner-7d9f-abc12"

    def test_falls_back_to_hostname(self, monkeypatch) -> None:
        monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
        monkeypatch.setenv("HOSTNAME", "runner-host")
        assert entrypoint._runner_instance_id() == "runner-host"

    def test_unset_returns_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
        monkeypatch.delenv("HOSTNAME", raising=False)
        assert entrypoint._runner_instance_id() == ""

    @pytest.mark.parametrize("bad", ["has space", "a" * 64, "-leading", "trailing.", "x/y"])
    def test_invalid_explicit_id_fails_startup(self, monkeypatch, bad) -> None:
        monkeypatch.setenv("RUNNER_INSTANCE_ID", bad)
        with pytest.raises(ValueError, match="RUNNER_INSTANCE_ID"):
            entrypoint._runner_instance_id()

    def test_invalid_hostname_skipped_with_warning(self, monkeypatch, caplog) -> None:
        """A HOSTNAME fallback that isn't a legal label value (e.g. a long
        FQDN on a dev machine) disables own-instance cleanup instead of
        failing startup."""
        monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
        monkeypatch.setenv("HOSTNAME", "h" * 64)
        with caplog.at_level("WARNING"):
            assert entrypoint._runner_instance_id() == ""
        assert "HOSTNAME" in caplog.text


class TestSandboxMaxLifetime:
    """_sandbox_max_lifetime_seconds() parses and validates SANDBOX_MAX_LIFETIME_SECONDS."""

    def test_default(self, monkeypatch) -> None:
        monkeypatch.delenv("SANDBOX_MAX_LIFETIME_SECONDS", raising=False)
        assert entrypoint._sandbox_max_lifetime_seconds() == 86400

    def test_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("SANDBOX_MAX_LIFETIME_SECONDS", "7200")
        assert entrypoint._sandbox_max_lifetime_seconds() == 7200

    @pytest.mark.parametrize("bad", ["abc", "3899", "0", "-5"])
    def test_invalid_or_below_floor_fails_startup(self, monkeypatch, bad) -> None:
        monkeypatch.setenv("SANDBOX_MAX_LIFETIME_SECONDS", bad)
        with pytest.raises(ValueError, match="SANDBOX_MAX_LIFETIME_SECONDS"):
            entrypoint._sandbox_max_lifetime_seconds()

    def test_floor_value_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("SANDBOX_MAX_LIFETIME_SECONDS", "3900")
        assert entrypoint._sandbox_max_lifetime_seconds() == 3900


class TestCreateSpawnerWiring:
    """_create_spawner() threads identity and lifetime into the spawner."""

    def test_owner_and_lifetime_passed_to_spawner(self, mocker, monkeypatch) -> None:
        mocker.patch.object(entrypoint, "SPAWNER_TYPE", "openshell")
        monkeypatch.setenv("SANDBOX_MAX_LIFETIME_SECONDS", "7200")
        build = mocker.patch("cloud_agents.spawner.factory.build_spawner")

        entrypoint._create_spawner(owner_instance="runner-a")

        assert build.call_args.kwargs["owner_instance"] == "runner-a"
        assert build.call_args.kwargs["max_lifetime_seconds"] == 7200

    def test_lifetime_below_floor_fails_startup(self, mocker, monkeypatch) -> None:
        mocker.patch.object(entrypoint, "SPAWNER_TYPE", "openshell")
        monkeypatch.setenv("SANDBOX_MAX_LIFETIME_SECONDS", "60")
        mocker.patch("cloud_agents.spawner.factory.build_spawner")

        with pytest.raises(ValueError, match="SANDBOX_MAX_LIFETIME_SECONDS"):
            entrypoint._create_spawner(owner_instance="runner-a")
