"""Tests for startup reconciliation of orphaned sandbox containers."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_agents.workflow.executor.temporal import entrypoint
from cloud_agents.workflow.executor.temporal.entrypoint import reconcile_orphaned_sandboxes

AGENT_KEY = "cloud-agents/agent-name"
EXPIRES_KEY = "cloud-agents/expires-at"
NOW = 1_700_000_000.0
PAST = str(int(NOW) - 1)
FUTURE = str(int(NOW) + 3600)


def _fake_spawner(sandboxes: dict[str, dict[str, str]]) -> AsyncMock:
    """Spawner whose list_sandbox_labels() returns sandboxes as-is.

    sandboxes maps gateway sandbox name -> labels beyond spawned-by (an empty
    dict is a legacy sandbox carrying only spawned-by=workflow-runner).
    """
    spawner = AsyncMock()

    async def list_sandbox_labels(labels: dict[str, str] | None = None):
        assert labels == {"spawned-by": "workflow-runner"}
        return [
            (name, {"spawned-by": "workflow-runner", **extra}) for name, extra in sandboxes.items()
        ]

    spawner.list_sandbox_labels.side_effect = list_sandbox_labels
    return spawner


def _destroyed(spawner: AsyncMock) -> set[str]:
    return {c.args[0] for c in spawner.destroy_sandbox.call_args_list}


class TestStartupReconciliation:
    """reconcile_orphaned_sandboxes() destroys expired sandboxes only."""

    @pytest.mark.asyncio
    async def test_expired_sandboxes_destroyed_by_exact_name(self) -> None:
        mock_spawner = _fake_spawner(
            {"sb-old1": {EXPIRES_KEY: PAST}, "sb-old2": {EXPIRES_KEY: PAST}}
        )

        await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.list_sandbox_labels.assert_called_once()
        mock_spawner.destroy.assert_not_called()
        assert _destroyed(mock_spawner) == {"sb-old1", "sb-old2"}

    @pytest.mark.asyncio
    async def test_unexpired_sandboxes_survive(self) -> None:
        mock_spawner = _fake_spawner({"sb-live": {EXPIRES_KEY: FUTURE}})

        await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.destroy_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_label_never_triggers_destroy(self) -> None:
        """A matching runner identity doesn't prove the previous owner is
        dead, so an owner-instance label (left by an earlier build) is
        ignored: unexpired sandboxes survive whatever their owner label."""
        mock_spawner = _fake_spawner(
            {"sb-live": {"cloud-agents/owner-instance": "runner-a", EXPIRES_KEY: FUTURE}}
        )

        await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.destroy_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_expires_at_kept_and_logged(self, caplog) -> None:
        mock_spawner = _fake_spawner({"sb-weird": {EXPIRES_KEY: "soon"}})

        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.destroy_sandbox.assert_not_called()
        assert "sb-weird" in caplog.text

    @pytest.mark.asyncio
    async def test_sandboxes_without_expiry_left_alone_and_logged(self, caplog) -> None:
        mock_spawner = _fake_spawner({"sb-legacy": {}, "sb-cli": {AGENT_KEY: "cli-1"}})

        with caplog.at_level("INFO"):
            await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.destroy_sandbox.assert_not_called()
        assert "2 sandbox(es)" in caplog.text

    @pytest.mark.asyncio
    async def test_no_orphans_no_destroy(self) -> None:
        mock_spawner = _fake_spawner({})

        await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        mock_spawner.destroy_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_destroy_failure_logged_not_raised(self) -> None:
        """If destroy fails for one orphan, others are still attempted."""
        mock_spawner = _fake_spawner({n: {EXPIRES_KEY: PAST} for n in ("o1", "o2", "o3")})
        mock_spawner.destroy_sandbox.side_effect = [None, RuntimeError("destroy failed"), None]

        # Should not raise
        await reconcile_orphaned_sandboxes(mock_spawner, now=NOW)

        assert mock_spawner.destroy_sandbox.call_count == 3

    @pytest.mark.asyncio
    async def test_none_spawner_is_noop(self) -> None:
        """reconcile_orphaned_sandboxes with None spawner does nothing."""
        # Should not raise
        await reconcile_orphaned_sandboxes(None)


class TestNoRunnerIdentity:
    """The owner-identity mechanism is gone: nothing resolves or stamps it."""

    def test_entrypoint_has_no_identity_resolution(self) -> None:
        assert not hasattr(entrypoint, "_runner_instance_id")

    def test_create_spawner_passes_no_owner(self, mocker, monkeypatch) -> None:
        mocker.patch.object(entrypoint, "SPAWNER_TYPE", "openshell")
        build = mocker.patch("cloud_agents.spawner.factory.build_spawner")

        entrypoint._create_spawner()

        assert "owner_instance" not in build.call_args.kwargs


class TestSandboxLifetimeSettings:
    """SANDBOX_MIN_LIFETIME_SECONDS / SANDBOX_EXPIRY_SKEW_SECONDS parsing."""

    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.delenv("SANDBOX_MIN_LIFETIME_SECONDS", raising=False)
        monkeypatch.delenv("SANDBOX_EXPIRY_SKEW_SECONDS", raising=False)
        assert entrypoint._sandbox_min_lifetime_seconds() == 86400
        assert entrypoint._sandbox_expiry_skew_seconds() == 600

    def test_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("SANDBOX_MIN_LIFETIME_SECONDS", "60")
        monkeypatch.setenv("SANDBOX_EXPIRY_SKEW_SECONDS", "0")
        assert entrypoint._sandbox_min_lifetime_seconds() == 60
        assert entrypoint._sandbox_expiry_skew_seconds() == 0

    @pytest.mark.parametrize("bad", ["abc", "-5", "1.5"])
    def test_invalid_min_lifetime_fails_startup(self, monkeypatch, bad) -> None:
        monkeypatch.setenv("SANDBOX_MIN_LIFETIME_SECONDS", bad)
        with pytest.raises(ValueError, match="SANDBOX_MIN_LIFETIME_SECONDS"):
            entrypoint._sandbox_min_lifetime_seconds()

    @pytest.mark.parametrize("bad", ["abc", "-1"])
    def test_invalid_skew_fails_startup(self, monkeypatch, bad) -> None:
        monkeypatch.setenv("SANDBOX_EXPIRY_SKEW_SECONDS", bad)
        with pytest.raises(ValueError, match="SANDBOX_EXPIRY_SKEW_SECONDS"):
            entrypoint._sandbox_expiry_skew_seconds()

    def test_settings_passed_to_spawner(self, mocker, monkeypatch) -> None:
        mocker.patch.object(entrypoint, "SPAWNER_TYPE", "openshell")
        monkeypatch.setenv("SANDBOX_MIN_LIFETIME_SECONDS", "7200")
        monkeypatch.setenv("SANDBOX_EXPIRY_SKEW_SECONDS", "45")
        build = mocker.patch("cloud_agents.spawner.factory.build_spawner")

        entrypoint._create_spawner()

        assert build.call_args.kwargs["min_lifetime_seconds"] == 7200
        assert build.call_args.kwargs["expiry_skew_seconds"] == 45


# --- Real OpenShellSpawner against a fake gateway ---------------------------


class _FakeRef:
    def __init__(self, name: str, labels: dict[str, str]):
        self.name = name
        self.id = f"id-{name}"
        self.labels = labels


class _FakeGateway:
    """Minimal stand-in for openshell.SandboxClient's sandbox CRUD."""

    def __init__(self, refs: list[_FakeRef] | None = None, create_delay: float = 0.0):
        self.refs = {r.name: r for r in refs or []}
        self.deleted: list[str] = []
        self.create_delay = create_delay
        self.created = threading.Event()
        self._n = 0

    def list(self, *, workspace, label_selector=None, limit=100, offset=0):
        wanted = dict(p.split("=", 1) for p in (label_selector or "").split(",") if p)
        matches = [
            r for r in self.refs.values() if all(r.labels.get(k) == v for k, v in wanted.items())
        ]
        return matches[offset : offset + limit]

    def delete(self, name, *, workspace):
        self.deleted.append(name)
        self.refs.pop(name, None)

    def create(self, *, workspace, spec, labels):
        time.sleep(self.create_delay)
        self._n += 1
        ref = _FakeRef(f"sb-{self._n}", dict(labels))
        self.refs[ref.name] = ref
        self.created.set()
        return ref

    def wait_ready(self, name, *, workspace, timeout_seconds):
        return self.refs[name]

    def exec_stream(self, *args, **kwargs):
        return iter([])


class _RecordingClock:
    """Real (advancing) wall clock that records each reading."""

    def __init__(self) -> None:
        self.readings: list[float] = []

    def __call__(self) -> float:
        value = time.time()
        self.readings.append(value)
        return value


def _real_spawner(mocker, gateway: _FakeGateway, **kwargs: Any):
    from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

    spawner = OpenShellSpawner(openshell_client=gateway, **kwargs)
    mocker.patch.object(spawner, "_expose_service", return_value=("http://gw", "host"))
    mocker.patch.object(spawner, "_wait_ready_with_host", return_value=True)
    mocker.patch.object(OpenShellSpawner, "_build_network_policy")
    return spawner


def _labels(agent: str, expires_at: str | None = None) -> dict[str, str]:
    labels = {"spawned-by": "workflow-runner", AGENT_KEY: agent}
    if expires_at is not None:
        labels[EXPIRES_KEY] = expires_at
    return labels


class TestExactDeletionRealSpawner:
    """Reconcile deletes exactly the expired gateway ref, never other refs
    that merely share its cloud-agents/agent-name label."""

    @pytest.mark.asyncio
    async def test_same_agent_name_only_expired_ref_deleted(self, mocker) -> None:
        gateway = _FakeGateway(
            [
                _FakeRef("sb-expired", _labels("ca-dup", PAST)),
                _FakeRef("sb-live", _labels("ca-dup", FUTURE)),
            ]
        )
        spawner = _real_spawner(mocker, gateway)

        await reconcile_orphaned_sandboxes(spawner, now=NOW)

        assert gateway.deleted == ["sb-expired"]

    @pytest.mark.asyncio
    async def test_expired_and_legacy_same_agent_only_expired_deleted(self, mocker) -> None:
        gateway = _FakeGateway(
            [
                _FakeRef("sb-expired", _labels("ca-dup", PAST)),
                _FakeRef("sb-legacy", _labels("ca-dup")),
            ]
        )
        spawner = _real_spawner(mocker, gateway)

        await reconcile_orphaned_sandboxes(spawner, now=NOW)

        assert gateway.deleted == ["sb-expired"]

    @pytest.mark.asyncio
    async def test_expired_and_unlabelled_legacy_only_expired_deleted(self, mocker) -> None:
        gateway = _FakeGateway(
            [
                _FakeRef("sb-expired", _labels("ca-a", PAST)),
                _FakeRef("sb-legacy", {"spawned-by": "workflow-runner"}),
            ]
        )
        spawner = _real_spawner(mocker, gateway)

        await reconcile_orphaned_sandboxes(spawner, now=NOW)

        assert gateway.deleted == ["sb-expired"]


class TestReconcileWithoutExactDestroy:
    @pytest.mark.asyncio
    async def test_spawner_without_destroy_sandbox_is_skipped_and_logged(self, caplog) -> None:
        """A spawner that lists labels but doesn't implement exact deletion is
        skipped, never falling back to agent-name destroy()."""
        from cloud_agents.spawner.base import AgentSpawner

        class ListOnlySpawner(AgentSpawner):
            destroyed: list[str] = []

            async def _do_spawn(self, *args, **kwargs):
                return ""

            async def _do_destroy(self, agent_name):
                self.destroyed.append(agent_name)

            async def _do_list_active(self, labels=None):
                return []

            async def _do_list_sandbox_labels(self, labels=None):
                return [("sb-expired", {EXPIRES_KEY: PAST})]

            async def _do_read_file(self, agent_name, path):
                raise FileNotFoundError(path)

            async def _do_write_file(self, agent_name, path, content):
                pass

        spawner = ListOnlySpawner()
        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(spawner, now=NOW)

        assert spawner.destroyed == []
        assert "sb-expired" in caplog.text


class TestCancelledSpawnRealSpawner:
    @pytest.mark.asyncio
    async def test_cancel_after_create_deletes_exact_sandbox(self, mocker) -> None:
        gateway = _FakeGateway()
        spawner = _real_spawner(mocker, gateway)

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        mocker.patch.object(spawner, "start_server", side_effect=hang)
        task = asyncio.create_task(spawner.spawn("ca-agent", "img", env={}, lifetime_seconds=60))
        assert await asyncio.to_thread(gateway.created.wait, 5)
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.deleted == ["sb-1"]
        assert spawner._active_count == 0
        assert spawner._sandbox_names == {}


class TestSpawnDeadlineRealSpawner:
    """The spawn budget starts when expires-at is stamped and bounds create
    plus every post-create phase, so a sandbox whose lifetime covers the
    budget is never reaped before its spawn could have finished using it.

    Uses min_lifetime=0, skew=0 and lifetime_seconds == the (patched)
    budget, so expires-at is exactly as early as the bound allows.
    """

    DEADLINE = 1

    @pytest.fixture(autouse=True)
    def _short_deadline(self, mocker):
        import cloud_agents.spawner.openshell_spawner as mod

        mocker.patch.object(mod, "SANDBOX_SPAWN_DEADLINE_SECONDS", self.DEADLINE)

    async def _spawn_expecting_timeout(self, spawner) -> float:
        start = time.monotonic()
        with pytest.raises(TimeoutError) as exc_info:
            await asyncio.wait_for(
                spawner.spawn("ca-agent", "img", env={}, lifetime_seconds=self.DEADLINE),
                timeout=10,
            )
        assert "deadline" in str(exc_info.value)
        return time.monotonic() - start

    async def _assert_not_reaped_before_budget_end(self, gateway, clock, name) -> None:
        stamp = clock.readings[0]
        expires_at = gateway.refs[name].labels[EXPIRES_KEY]
        reader = _FakeGateway([gateway.refs[name]])
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        other = OpenShellSpawner(openshell_client=reader)
        await reconcile_orphaned_sandboxes(other, now=stamp + self.DEADLINE)
        assert reader.deleted == []
        await reconcile_orphaned_sandboxes(other, now=int(expires_at) + 1)
        assert reader.deleted == [name]

    @pytest.mark.asyncio
    async def test_trickling_health_check_aborted_by_deadline(self, mocker) -> None:
        gateway = _FakeGateway()
        clock = _RecordingClock()
        spawner = _real_spawner(
            mocker, gateway, min_lifetime_seconds=0, expiry_skew_seconds=0, clock=clock
        )

        async def trickle(*args, **kwargs):
            await asyncio.sleep(3600)

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=trickle)
        created = {}
        real_create = gateway.create

        def create(**kwargs):
            ref = real_create(**kwargs)
            created["ref"] = _FakeRef(ref.name, dict(ref.labels))
            return ref

        gateway.create = create

        elapsed = await self._spawn_expecting_timeout(spawner)

        assert elapsed < self.DEADLINE + 1
        assert gateway.deleted == [created["ref"].name]  # exact, known identity
        gateway.refs[created["ref"].name] = created["ref"]
        await self._assert_not_reaped_before_budget_end(gateway, clock, created["ref"].name)

    @pytest.mark.asyncio
    async def test_slow_post_create_step_aborted_by_deadline(self, mocker) -> None:
        gateway = _FakeGateway()
        clock = _RecordingClock()
        spawner = _real_spawner(
            mocker, gateway, min_lifetime_seconds=0, expiry_skew_seconds=0, clock=clock
        )

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        mocker.patch.object(spawner, "start_server", side_effect=hang)

        elapsed = await self._spawn_expecting_timeout(spawner)

        assert elapsed < self.DEADLINE + 1
        assert gateway.deleted == ["sb-1"]

    @pytest.mark.asyncio
    async def test_slow_create_sandbox_bounded_and_left_to_expiry(self, mocker) -> None:
        """CreateSandbox outlives the budget: spawn still returns by the
        deadline. Cancelling the awaiting coroutine doesn't stop the blocking
        RPC, so the sandbox may still appear afterwards -- with an expiry no
        earlier than the budget end, so it's reaped only once expired. With
        its identity unknown, no delete is guessed."""
        gateway = _FakeGateway(create_delay=self.DEADLINE + 1)
        clock = _RecordingClock()
        spawner = _real_spawner(
            mocker, gateway, min_lifetime_seconds=0, expiry_skew_seconds=0, clock=clock
        )

        elapsed = await self._spawn_expecting_timeout(spawner)

        assert elapsed < self.DEADLINE + 1
        assert gateway.deleted == []
        assert await asyncio.to_thread(gateway.created.wait, 5)
        await self._assert_not_reaped_before_budget_end(gateway, clock, "sb-1")
