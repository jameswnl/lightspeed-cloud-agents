"""Tests for startup reconciliation of orphaned sandbox containers."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_agents.workflow.executor.temporal import entrypoint
from cloud_agents.workflow.executor.temporal.entrypoint import reconcile_orphaned_sandboxes

AGENT_KEY = "cloud-agents/agent-name"
EXPIRES_KEY = "cloud-agents/expires-at"
NOW = 1_700_000_000.0
PAST = str(int(NOW) - 1)
FUTURE = str(int(NOW) + 3600)


def _fake_spawner(sandboxes: dict[str, dict[str, str]]) -> AsyncMock:
    """Spawner whose list_sandbox_labels_page() returns sandboxes as one page.

    sandboxes maps gateway sandbox name -> labels beyond spawned-by (an empty
    dict is a legacy sandbox carrying only spawned-by=workflow-runner).
    """
    spawner = AsyncMock()

    async def list_sandbox_labels_page(labels: dict[str, str] | None = None, offset: int = 0):
        assert labels == {"spawned-by": "workflow-runner"}
        assert offset == 0
        pairs = [
            (name, {"spawned-by": "workflow-runner", **extra}) for name, extra in sandboxes.items()
        ]
        return pairs, None

    spawner.list_sandbox_labels_page.side_effect = list_sandbox_labels_page
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

        mock_spawner.list_sandbox_labels_page.assert_called_once()
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
    budget + cancel grace, so expires-at is exactly as early as the bound
    allows.
    """

    DEADLINE = 1
    GRACE = 1
    STUBBORN = 2.5  # how long "uncooperative" code ignores cancellation

    @pytest.fixture(autouse=True)
    def _short_deadline(self, mocker):
        import cloud_agents.spawner.openshell_spawner as mod
        from cloud_agents.spawner import lifetime

        mocker.patch.object(mod, "SANDBOX_SPAWN_DEADLINE_SECONDS", self.DEADLINE)
        mocker.patch.object(lifetime, "SANDBOX_CANCEL_GRACE_SECONDS", self.GRACE)

    async def _spawn_expecting_timeout(self, spawner) -> float:
        start = time.monotonic()
        with pytest.raises(TimeoutError) as exc_info:
            await asyncio.wait_for(
                spawner.spawn(
                    "ca-agent", "img", env={}, lifetime_seconds=self.DEADLINE + self.GRACE
                ),
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
        await reconcile_orphaned_sandboxes(other, now=stamp + self.DEADLINE + self.GRACE)
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

    @staticmethod
    async def _stubborn_sleep(seconds: float) -> None:
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            try:
                await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_readiness_close_ignoring_cancellation_bounded_by_grace(self, mocker) -> None:
        """The readiness check overruns the budget and its client close then
        ignores cancellation: spawn still raises within budget + grace, and
        deletes the exact sandbox it created."""
        gateway = _FakeGateway()
        clock = _RecordingClock()
        spawner = _real_spawner(
            mocker, gateway, min_lifetime_seconds=0, expiry_skew_seconds=0, clock=clock
        )

        async def readiness_with_stuck_close(*args, **kwargs):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await self._stubborn_sleep(self.STUBBORN)  # e.g. client __aexit__
                raise

        mocker.patch.object(
            spawner, "_wait_ready_with_host", side_effect=readiness_with_stuck_close
        )
        snapshot = {}
        real_create = gateway.create

        def create(**kwargs):
            ref = real_create(**kwargs)
            snapshot["ref"] = _FakeRef(ref.name, dict(ref.labels))
            return ref

        gateway.create = create

        elapsed = await self._spawn_expecting_timeout(spawner)

        assert elapsed < self.DEADLINE + self.GRACE + 0.5
        assert gateway.deleted == ["sb-1"]
        gateway.refs["sb-1"] = snapshot["ref"]
        await self._assert_not_reaped_before_budget_end(gateway, clock, "sb-1")
        await asyncio.sleep(self.STUBBORN)  # let the abandoned close finish

    @pytest.mark.asyncio
    async def test_stuck_failure_cleanup_bounded_and_logged(self, mocker, caplog) -> None:
        """A failure-path cleanup that hangs (ignoring cancellation) is
        abandoned and logged; the caller gets the original error promptly."""
        import cloud_agents.spawner.openshell_spawner as mod

        mocker.patch.object(mod, "SANDBOX_SPAWN_CLEANUP_TIMEOUT_SECONDS", 0.2, create=True)
        gateway = _FakeGateway()
        spawner = _real_spawner(mocker, gateway)
        mocker.patch.object(spawner, "start_server", side_effect=RuntimeError("boom"))

        async def stuck_cleanup(*args, **kwargs):
            await self._stubborn_sleep(self.STUBBORN)

        mocker.patch.object(spawner, "_cleanup_sandbox", side_effect=stuck_cleanup)

        start = time.monotonic()
        with caplog.at_level("WARNING"), pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(
                spawner.spawn("ca-agent", "img", env={}, lifetime_seconds=60), timeout=10
            )

        assert time.monotonic() - start < 0.2 + self.GRACE + 0.5
        assert "abandon" in caplog.text
        await asyncio.sleep(self.STUBBORN)

    @pytest.mark.asyncio
    async def test_own_timeout_error_not_reported_as_deadline(self, mocker) -> None:
        """A TimeoutError raised by a spawn phase itself, well before the
        budget expires, propagates as-is rather than as a deadline overrun."""
        gateway = _FakeGateway()
        spawner = _real_spawner(mocker, gateway)
        mocker.patch.object(spawner, "start_server", side_effect=TimeoutError("exec timed out"))

        with pytest.raises(TimeoutError) as exc_info:
            await asyncio.wait_for(
                spawner.spawn("ca-agent", "img", env={}, lifetime_seconds=60), timeout=10
            )

        assert str(exc_info.value) == "exec timed out"
        assert gateway.deleted == ["sb-1"]


class TestMcpSecretsInsideSpawnBudget:
    """MCP secret files (#277) are written inside the spawn budget, so their
    failures go through the bounded, exact-sandbox cleanup."""

    MOUNTS = [("MCP_TOKEN", "token", "/run/mcp/token")]

    @pytest.mark.asyncio
    async def test_failed_secret_write_deletes_exact_sandbox(self, mocker) -> None:
        gateway = _FakeGateway()
        spawner = _real_spawner(mocker, gateway)
        mocker.patch.object(spawner, "_exec_mkdir", return_value=None)
        mocker.patch.object(spawner, "_write_secret_file", side_effect=RuntimeError("write failed"))

        with pytest.raises(RuntimeError, match="write failed"):
            await spawner.spawn(
                "ca-agent",
                "img",
                env={"MCP_TOKEN": "s3cret"},
                lifetime_seconds=60,
                mcp_secret_mounts=self.MOUNTS,
            )

        assert gateway.deleted == ["sb-1"]
        assert spawner._active_count == 0

    @pytest.mark.asyncio
    async def test_hanging_secret_write_bounded_by_spawn_budget(self, mocker) -> None:
        import cloud_agents.spawner.openshell_spawner as mod

        mocker.patch.object(mod, "SANDBOX_SPAWN_DEADLINE_SECONDS", 1)
        gateway = _FakeGateway()
        spawner = _real_spawner(mocker, gateway)
        mocker.patch.object(spawner, "_exec_mkdir", return_value=None)

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        mocker.patch.object(spawner, "_write_secret_file", side_effect=hang)

        start = time.monotonic()
        with pytest.raises(TimeoutError, match="deadline"):
            await asyncio.wait_for(
                spawner.spawn(
                    "ca-agent",
                    "img",
                    env={"MCP_TOKEN": "s3cret"},
                    lifetime_seconds=60,
                    mcp_secret_mounts=self.MOUNTS,
                ),
                timeout=10,
            )

        assert time.monotonic() - start < 2
        assert gateway.deleted == ["sb-1"]

    @pytest.mark.asyncio
    async def test_secret_key_kept_out_of_spec_environment(self, mocker) -> None:
        gateway = _FakeGateway()
        spawner = _real_spawner(mocker, gateway)
        mocker.patch.object(spawner, "_exec_mkdir", return_value=None)
        mocker.patch.object(spawner, "_write_secret_file", return_value=None)
        seen: dict[str, dict[str, str]] = {}
        real_create = gateway.create

        def create(**kwargs):
            environment = kwargs["spec"].environment
            if isinstance(environment, MagicMock):
                # test_openshell_spawner.py may have stubbed `openshell` for
                # the whole session; read the map writes off the mock.
                seen["env"] = {c.args[0]: c.args[1] for c in environment.__setitem__.call_args_list}
            else:
                seen["env"] = dict(environment)
            return real_create(**kwargs)

        gateway.create = create

        await spawner.spawn(
            "ca-agent",
            "img",
            env={"MCP_TOKEN": "s3cret", "PLAIN": "ok"},
            lifetime_seconds=60,
            mcp_secret_mounts=self.MOUNTS,
        )

        assert "MCP_TOKEN" not in seen["env"]
        assert seen["env"]["PLAIN"] == "ok"


# --- Periodic expired-only reaper -------------------------------------------


class _AdvancingClock:
    """Fake clock: each reading is `step` seconds after the previous one."""

    def __init__(self, start: float = NOW, step: float = 10.0) -> None:
        self.readings: list[float] = []
        self._next = start
        self._step = step

    def __call__(self) -> float:
        value = self._next
        self._next += self._step
        self.readings.append(value)
        return value


class _NotFound(Exception):
    """Stand-in for the grpc.RpcError DeleteSandbox raises for a missing name."""

    def code(self):
        import grpc

        return grpc.StatusCode.NOT_FOUND


class _SharedGateway(_FakeGateway):
    """Gateway whose DeleteSandbox fails with NOT_FOUND for a missing name,
    like a sandbox another replica already deleted."""

    def delete(self, name, *, workspace):
        if name not in self.refs:
            import grpc

            class NotFound(grpc.RpcError, _NotFound):
                pass

            raise NotFound()
        super().delete(name, workspace=workspace)


class _HangingListGateway(_FakeGateway):
    """Gateway whose list calls numbered in `hang_on` block until released."""

    def __init__(self, refs: list[_FakeRef] | None = None, hang_on: tuple[int, ...] = (1,)):
        super().__init__(refs)
        self.release = threading.Event()
        self.list_calls = 0
        self._hang_on = hang_on

    def list(self, **kwargs):
        self.list_calls += 1
        if self.list_calls in self._hang_on:
            self.release.wait(10)
        return super().list(**kwargs)


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


def _counter(name: str, labels: dict[str, str] | None = None) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


INTERVAL = 0.02


class TestPeriodicReaper:
    """start_sandbox_reaper() re-runs reconcile_orphaned_sandboxes() every
    interval, so orphans expire into deletion without a runner restart."""

    @pytest.fixture(autouse=True)
    def _short_bounds(self, mocker):
        from cloud_agents.spawner import lifetime

        mocker.patch.object(lifetime, "SANDBOX_CANCEL_GRACE_SECONDS", 0.2)
        mocker.patch.object(entrypoint, "SANDBOX_REAP_CALL_TIMEOUT_SECONDS", 0.2)

    @pytest.mark.asyncio
    async def test_expired_reaped_on_later_tick(self, mocker) -> None:
        gateway = _FakeGateway([_FakeRef("sb-soon", _labels("ca-a", str(int(NOW) + 25)))])
        spawner = _real_spawner(mocker, gateway)
        clock = _AdvancingClock()

        task = entrypoint.start_sandbox_reaper(spawner, INTERVAL, clock=clock)
        try:
            await _until(lambda: gateway.deleted)
        finally:
            await entrypoint.stop_sandbox_reaper(task)

        assert gateway.deleted == ["sb-soon"]
        # Not reaped on the ticks at NOW, NOW+10, NOW+20, only once past expiry.
        assert len(clock.readings) >= 4

    @pytest.mark.asyncio
    async def test_unexpired_legacy_and_malformed_kept(self, mocker) -> None:
        gateway = _FakeGateway(
            [
                _FakeRef("sb-expired", _labels("ca-a", PAST)),
                _FakeRef("sb-live", _labels("ca-b", str(int(NOW) + 10**6))),
                _FakeRef("sb-legacy", {"spawned-by": "workflow-runner"}),
                _FakeRef("sb-noexpiry", _labels("ca-c")),
                _FakeRef("sb-weird", _labels("ca-d", "soon")),
            ]
        )
        spawner = _real_spawner(mocker, gateway)
        clock = _AdvancingClock()

        task = entrypoint.start_sandbox_reaper(spawner, INTERVAL, clock=clock)
        try:
            await _until(lambda: len(clock.readings) >= 4)
        finally:
            await entrypoint.stop_sandbox_reaper(task)

        assert gateway.deleted == ["sb-expired"]
        assert set(gateway.refs) == {"sb-live", "sb-legacy", "sb-noexpiry", "sb-weird"}

    @pytest.mark.asyncio
    async def test_overlapping_tick_skipped(self, mocker, caplog) -> None:
        """A tick that fires while the previous run is still in progress is
        skipped rather than starting a second, concurrent run."""
        mocker.patch.object(entrypoint, "SANDBOX_REAP_CALL_TIMEOUT_SECONDS", 30)
        gateway = _HangingListGateway([_FakeRef("sb-expired", _labels("ca-a", PAST))])
        spawner = _real_spawner(mocker, gateway)

        with caplog.at_level("WARNING"):
            task = entrypoint.start_sandbox_reaper(spawner, INTERVAL, clock=_AdvancingClock())
            try:
                await asyncio.sleep(INTERVAL * 10)
                assert gateway.list_calls == 1
                assert "previous run still in progress" in caplog.text
                gateway.release.set()
                await _until(lambda: gateway.deleted)
            finally:
                gateway.release.set()
                await entrypoint.stop_sandbox_reaper(task)

        assert gateway.deleted == ["sb-expired"]

    @pytest.mark.asyncio
    async def test_hung_list_bounded_and_counted(self, mocker, caplog) -> None:
        """A list call that never returns is abandoned at its deadline; the
        failure is logged and counted, and a later run still reaps."""
        gateway = _HangingListGateway([_FakeRef("sb-expired", _labels("ca-a", PAST))])
        spawner = _real_spawner(mocker, gateway)
        before = _counter("ls_sandbox_reap_failures_total", {"operation": "list"})

        with caplog.at_level("ERROR"):
            task = entrypoint.start_sandbox_reaper(spawner, INTERVAL, clock=_AdvancingClock())
            try:
                await _until(lambda: gateway.deleted)
            finally:
                await entrypoint.stop_sandbox_reaper(task)
                gateway.release.set()

        assert gateway.deleted == ["sb-expired"]
        assert gateway.list_calls >= 2
        assert _counter("ls_sandbox_reap_failures_total", {"operation": "list"}) == before + 1
        assert "Sandbox reaper run failed" in caplog.text

    @pytest.mark.asyncio
    async def test_hung_delete_bounded_and_counted(self, mocker) -> None:
        release = threading.Event()
        gateway = _FakeGateway(
            [
                _FakeRef("sb-stuck", _labels("ca-a", PAST)),
                _FakeRef("sb-ok", _labels("ca-b", PAST)),
            ]
        )
        real_delete = gateway.delete

        def delete(name, *, workspace):
            if name == "sb-stuck":
                release.wait(10)
            real_delete(name, workspace=workspace)

        gateway.delete = delete
        spawner = _real_spawner(mocker, gateway)
        before = _counter("ls_sandbox_reap_failures_total", {"operation": "delete"})

        start = time.monotonic()
        try:
            await reconcile_orphaned_sandboxes(spawner, now=NOW)
        finally:
            release.set()

        assert time.monotonic() - start < 2
        assert "sb-ok" in gateway.deleted
        assert _counter("ls_sandbox_reap_failures_total", {"operation": "delete"}) == before + 1

    @pytest.mark.asyncio
    async def test_concurrent_replicas_not_found_is_success(self, mocker, caplog) -> None:
        """Two replicas reap the same expired sandbox: whichever deletes it
        second gets NOT_FOUND, which counts as success, not a failure."""
        gateway = _SharedGateway([_FakeRef("sb-expired", _labels("ca-a", PAST))])
        replica_a = _real_spawner(mocker, gateway)
        replica_b = _real_spawner(mocker, gateway)
        # Both replicas list before either deletes.
        listed = [(r.name, dict(r.labels)) for r in gateway.refs.values()]
        mocker.patch.object(replica_a, "list_sandbox_labels_page", return_value=(listed, None))
        mocker.patch.object(replica_b, "list_sandbox_labels_page", return_value=(listed, None))
        before = _counter("ls_sandbox_reap_failures_total", {"operation": "delete"})

        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(replica_a, now=NOW, trigger="reaper")
            await reconcile_orphaned_sandboxes(replica_b, now=NOW, trigger="reaper")

        assert gateway.deleted == ["sb-expired"]
        assert _counter("ls_sandbox_reap_failures_total", {"operation": "delete"}) == before
        assert "Failed to destroy" not in caplog.text

    @pytest.mark.asyncio
    async def test_destroy_sandbox_not_found_does_not_raise(self, mocker) -> None:
        spawner = _real_spawner(mocker, _SharedGateway())

        await spawner.destroy_sandbox("sb-gone")

    @pytest.mark.asyncio
    async def test_destroy_sandbox_other_rpc_error_raises(self, mocker) -> None:
        import grpc

        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        gateway = _FakeGateway()
        gateway.delete = MagicMock(side_effect=Unavailable())
        spawner = _real_spawner(mocker, gateway)

        with pytest.raises(grpc.RpcError):
            await spawner.destroy_sandbox("sb-x")

    @pytest.mark.asyncio
    async def test_disabled_when_interval_zero(self, mocker) -> None:
        spawner = _real_spawner(mocker, _FakeGateway())

        assert entrypoint.start_sandbox_reaper(spawner, 0) is None
        assert entrypoint.start_sandbox_reaper(None, INTERVAL) is None
        await entrypoint.stop_sandbox_reaper(None)

    @pytest.mark.asyncio
    async def test_shutdown_cancels_idle_reaper(self, mocker) -> None:
        spawner = _real_spawner(mocker, _FakeGateway())
        task = entrypoint.start_sandbox_reaper(spawner, 3600)

        await entrypoint.stop_sandbox_reaper(task)

        assert task.done()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_mid_run_within_grace(self, mocker) -> None:
        mocker.patch.object(entrypoint, "SANDBOX_REAP_CALL_TIMEOUT_SECONDS", 30)
        gateway = _HangingListGateway([_FakeRef("sb-expired", _labels("ca-a", PAST))])
        spawner = _real_spawner(mocker, gateway)
        task = entrypoint.start_sandbox_reaper(spawner, INTERVAL, clock=_AdvancingClock())
        await _until(lambda: gateway.list_calls == 1)

        start = time.monotonic()
        try:
            await entrypoint.stop_sandbox_reaper(task)
        finally:
            gateway.release.set()

        assert time.monotonic() - start < 1
        assert task.done()
        assert gateway.deleted == []


class TestPagedListing:
    """Each ListSandboxes page is its own bounded call, and a run lists at
    most SANDBOX_REAP_MAX_PAGES pages, so a large inventory on a slow
    gateway still gets reaped page by page."""

    @pytest.fixture(autouse=True)
    def _small_pages(self, mocker):
        from cloud_agents.spawner import lifetime
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mocker.patch.object(OpenShellSpawner, "_LIST_PAGE_SIZE", 2)
        mocker.patch.object(lifetime, "SANDBOX_CANCEL_GRACE_SECONDS", 0.2)
        mocker.patch.object(entrypoint, "SANDBOX_REAP_CALL_TIMEOUT_SECONDS", 0.5)

    @staticmethod
    def _expired(n: int) -> list[_FakeRef]:
        return [_FakeRef(f"sb-{i}", _labels(f"ca-{i}", PAST)) for i in range(n)]

    @pytest.mark.asyncio
    async def test_each_page_gets_its_own_budget(self, mocker) -> None:
        """Pages that together exceed one call budget all get listed."""
        gateway = _FakeGateway(self._expired(5))
        real_list = gateway.list

        def slow_list(**kwargs):
            time.sleep(0.3)  # 3 pages x 0.3s > the 0.5s per-call budget
            return real_list(**kwargs)

        gateway.list = slow_list
        spawner = _real_spawner(mocker, gateway)

        await reconcile_orphaned_sandboxes(spawner, now=NOW)

        assert sorted(gateway.deleted) == [f"sb-{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_hung_later_page_reaps_listed_pages_then_raises(self, mocker) -> None:
        from cloud_agents.spawner.lifetime import SandboxDeadlineExceeded

        gateway = _HangingListGateway(self._expired(5), hang_on=(2,))
        spawner = _real_spawner(mocker, gateway)
        before = _counter("ls_sandbox_reap_failures_total", {"operation": "list"})

        start = time.monotonic()
        try:
            with pytest.raises(SandboxDeadlineExceeded):
                await reconcile_orphaned_sandboxes(spawner, now=NOW)
        finally:
            gateway.release.set()

        assert time.monotonic() - start < 2
        assert sorted(gateway.deleted) == ["sb-0", "sb-1"]
        assert _counter("ls_sandbox_reap_failures_total", {"operation": "list"}) == before + 1

    @pytest.mark.asyncio
    async def test_page_cap_reaps_listed_and_logs(self, mocker, caplog) -> None:
        mocker.patch.object(entrypoint, "SANDBOX_REAP_MAX_PAGES", 2)
        gateway = _FakeGateway(self._expired(5))
        spawner = _real_spawner(mocker, gateway)

        with caplog.at_level("WARNING"):
            await reconcile_orphaned_sandboxes(spawner, now=NOW)

        assert sorted(gateway.deleted) == ["sb-0", "sb-1", "sb-2", "sb-3"]
        assert "2 page(s)" in caplog.text

    @pytest.mark.asyncio
    async def test_base_spawner_lists_as_one_page(self) -> None:
        from cloud_agents.spawner.base import AgentSpawner

        class Minimal(AgentSpawner):
            async def _do_spawn(self, *args, **kwargs):
                return ""

            async def _do_destroy(self, agent_name):
                pass

            async def _do_list_active(self, labels=None):
                return ["a1"]

            async def _do_read_file(self, agent_name, path):
                raise FileNotFoundError(path)

            async def _do_write_file(self, agent_name, path, content):
                pass

        assert await Minimal().list_sandbox_labels_page() == ([("a1", {})], None)


class TestReaperIntervalSetting:
    def test_default(self, monkeypatch) -> None:
        monkeypatch.delenv("SANDBOX_REAP_INTERVAL_SECONDS", raising=False)
        assert entrypoint._sandbox_reap_interval_seconds() == 300

    def test_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("SANDBOX_REAP_INTERVAL_SECONDS", "0")
        assert entrypoint._sandbox_reap_interval_seconds() == 0

    @pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
    def test_invalid_fails_startup(self, monkeypatch, bad) -> None:
        monkeypatch.setenv("SANDBOX_REAP_INTERVAL_SECONDS", bad)
        with pytest.raises(ValueError, match="SANDBOX_REAP_INTERVAL_SECONDS"):
            entrypoint._sandbox_reap_interval_seconds()


def _reaper_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name() == "sandbox-reaper" and not t.done()]


class TestReaperInLifespans:
    """Both entrypoints start the reaper after startup reconcile and cancel
    it on shutdown."""

    @pytest.mark.asyncio
    async def test_local_lifespan(self, mocker, monkeypatch) -> None:
        from cloud_agents.workflow.executor.local import entrypoint as local_entrypoint

        monkeypatch.setenv("SANDBOX_REAP_INTERVAL_SECONDS", "3600")
        mocker.patch.object(local_entrypoint, "_create_spawner", return_value=_fake_spawner({}))
        app = local_entrypoint.build_local_app()

        async with app.router.lifespan_context(app):
            assert len(_reaper_tasks()) == 1
        assert _reaper_tasks() == []

    @pytest.mark.asyncio
    async def test_temporal_lifespan(self, mocker, monkeypatch) -> None:
        monkeypatch.setenv("SANDBOX_REAP_INTERVAL_SECONDS", "3600")
        monkeypatch.delenv("TRANSCRIPT_DB_URL", raising=False)
        mocker.patch.object(entrypoint, "_create_spawner", return_value=_fake_spawner({}))
        mocker.patch.object(entrypoint.Client, "connect", side_effect=RuntimeError("no temporal"))
        app = entrypoint.build_temporal_app()

        async with app.router.lifespan_context(app):
            assert len(_reaper_tasks()) == 1
        assert _reaper_tasks() == []

    @pytest.mark.asyncio
    async def test_temporal_lifespan_cancelled_during_startup_leaks_no_reaper(
        self, mocker, monkeypatch
    ) -> None:
        """Shutdown while the transcript store is still connecting must not
        leave a reaper task running."""
        monkeypatch.setenv("SANDBOX_REAP_INTERVAL_SECONDS", "3600")
        connecting = asyncio.Event()

        async def hang_connect():
            connecting.set()
            await asyncio.sleep(3600)

        store = MagicMock()
        store.connect = hang_connect
        store.close = AsyncMock()
        mocker.patch.object(entrypoint.TranscriptStore, "from_env", return_value=store)
        mocker.patch.object(entrypoint, "_create_spawner", return_value=_fake_spawner({}))
        app = entrypoint.build_temporal_app()

        async def run_lifespan():
            async with app.router.lifespan_context(app):
                pass

        task = asyncio.create_task(run_lifespan())
        await asyncio.wait_for(connecting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert _reaper_tasks() == []

    @pytest.mark.asyncio
    async def test_lifespan_reaper_disabled(self, mocker, monkeypatch) -> None:
        from cloud_agents.workflow.executor.local import entrypoint as local_entrypoint

        monkeypatch.setenv("SANDBOX_REAP_INTERVAL_SECONDS", "0")
        mocker.patch.object(local_entrypoint, "_create_spawner", return_value=_fake_spawner({}))
        app = local_entrypoint.build_local_app()

        async with app.router.lifespan_context(app):
            assert _reaper_tasks() == []
