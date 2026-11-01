"""Wall-clock bounds on sandbox HTTP calls, so a sandbox never outlives the
lifetime its expires-at label promises (cloud_agents.spawner.lifetime)."""

from __future__ import annotations

import asyncio
import importlib
import os
import time
from typing import Any

import pytest
from pytest_mock import MockerFixture

RUNNER_MODULES = [
    "cloud_agents.workflow.executor.temporal.activities",
    "cloud_agents.workflow.core.step_runner",
]


def _patch_client(mocker: MockerFixture, module: str, client: Any):
    client_cls = mocker.patch(f"{module}.httpx.AsyncClient")
    client_cls.return_value.__aenter__ = mocker.AsyncMock(return_value=client)
    client_cls.return_value.__aexit__ = mocker.AsyncMock(return_value=False)
    return client_cls


async def _bounded(coro: Any, seconds: float = 5) -> Any:
    """Await coro, failing the test (not hanging it) if it outlives seconds."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.cancel()
        pytest.fail(f"call still running after {seconds}s -- no wall-clock bound")
    return task.result()


class TestTranscriptCollectionTimeout:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("module", RUNNER_MODULES)
    async def test_uses_short_transcript_timeout(self, mocker: MockerFixture, module: str) -> None:
        """The events GET doesn't inherit the (step-sized) run timeout."""
        from cloud_agents.spawner.lifetime import SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS

        mod = importlib.import_module(module)
        response = mocker.MagicMock(status_code=404)
        client = mocker.MagicMock(get=mocker.AsyncMock(return_value=response))
        client_cls = _patch_client(mocker, module, client)

        await mod._collect_transcript(
            "http://pod:8080", "s", {"timeout": 200_000.0, "verify": False}, {}
        )

        kwargs = client_cls.call_args.kwargs
        assert kwargs["timeout"] == SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS
        assert kwargs["verify"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("module", RUNNER_MODULES)
    async def test_hanging_events_endpoint_released_within_bound(
        self, mocker: MockerFixture, module: str
    ) -> None:
        """A sandbox whose events endpoint never answers (or trickles bytes
        under the per-read timeout) is given up on after the wall-clock
        transcript bound, returning an empty transcript."""
        mod = importlib.import_module(module)
        mocker.patch.object(mod, "SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS", 0.05)

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        client = mocker.MagicMock(get=hang)
        _patch_client(mocker, module, client)

        start = time.monotonic()
        transcript = await _bounded(
            mod._collect_transcript("http://pod:8080", "s", {"timeout": 600}, {})
        )

        assert time.monotonic() - start < 2
        assert transcript.events == []


class TestStepRunnerPostDeadline:
    @pytest.mark.asyncio
    async def test_trickling_post_bounded_by_http_timeout(self, mocker: MockerFixture) -> None:
        """httpx's timeout is per-read, so a sandbox trickling bytes could hold
        the POST open forever; the local engine enforces http_timeout as an
        overall deadline and still destroys the sandbox."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        from cloud_agents.workflow.core.step_runner import run_step

        spawner = mocker.AsyncMock()
        spawner.spawn.return_value = "http://pod-1:8080"
        spawner.wait_ready.return_value = True
        spawner.get_query_ssl_context = mocker.Mock(return_value=None)

        async def trickle(*args, **kwargs):
            await asyncio.sleep(3600)

        client = mocker.MagicMock(post=trickle)
        _patch_client(mocker, "cloud_agents.workflow.core.step_runner", client)

        step_input = {
            "step": {
                "name": "slow",
                "prompt": "p",
                "output_key": "o",
                "permissions": {"timeout_seconds": 0.05},
            },
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await _bounded(run_step(step_input, spawner=spawner, attempt=1))

        assert time.monotonic() - start < 2
        spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_with_credential_gives_clear_redacted_error(
        self, mocker: MockerFixture
    ) -> None:
        """With a provider credential set, errors go through secret redaction;
        a timeout must still surface a non-empty message (str(TimeoutError())
        is empty) without leaking the credential, and be audited."""
        secret = "sk-live-very-secret-value"
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False)
        from cloud_agents.workflow.core import step_runner

        audit = mocker.patch.object(step_runner, "emit_audit")
        spawner = mocker.AsyncMock()
        spawner.spawn.return_value = "http://pod-1:8080"
        spawner.wait_ready.return_value = True
        spawner.get_query_ssl_context = mocker.Mock(return_value=None)

        async def trickle(*args, **kwargs):
            await asyncio.sleep(3600)

        _patch_client(
            mocker, "cloud_agents.workflow.core.step_runner", mocker.MagicMock(post=trickle)
        )

        step_input = {
            "step": {
                "name": "slow",
                "prompt": "p",
                "output_key": "o",
                "permissions": {"timeout_seconds": 0.05},
            },
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "OPENAI_API_KEY",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

        with pytest.raises(Exception) as exc_info:
            await _bounded(step_runner.run_step(step_input, spawner=spawner, attempt=1))

        message = str(exc_info.value)
        assert "timed out after 0.05s" in message
        assert secret not in message
        spawner.destroy.assert_called_once()
        timeout_events = [
            c.kwargs
            for c in audit.call_args_list
            if c.kwargs.get("event_type") == "sandbox_timeout"
        ]
        assert timeout_events and timeout_events[0]["details"]["reason"] == "timeout"


class TestTemporalPostDeadline:
    @pytest.mark.asyncio
    async def test_trickling_post_bounded_with_clear_redacted_error(
        self, mocker: MockerFixture
    ) -> None:
        """The Temporal activity enforces http_timeout locally too, rather than
        relying on heartbeat-delivered cancellation."""
        secret = "sk-live-very-secret-value"
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False)
        from cloud_agents.workflow.executor.temporal import activities

        audit = mocker.patch.object(activities, "emit_audit")
        spawner = mocker.AsyncMock()
        spawner.spawn.return_value = "http://pod-1:8080"
        spawner.wait_ready.return_value = True
        spawner.get_query_ssl_context = mocker.Mock(return_value=None)

        async def trickle(*args, **kwargs):
            await asyncio.sleep(3600)

        _patch_client(mocker, activities.__name__, mocker.MagicMock(post=trickle))

        with pytest.raises(Exception) as exc_info:
            await _bounded(
                activities.run_sandbox_step(
                    {
                        "step": {
                            "name": "slow",
                            "prompt": "p",
                            "output_key": "o",
                            "permissions": {"timeout_seconds": 0.05},
                        },
                        "workflow_id": "wf-1",
                        "provider": {
                            "name": "openai",
                            "model": "gpt-4o",
                            "credentials_secret": "OPENAI_API_KEY",
                        },
                        "sandbox_image": "sandbox:latest",
                        "context": {},
                    },
                    spawner=spawner,
                )
            )

        message = str(exc_info.value)
        assert "timed out after 0.05s" in message
        assert secret not in message
        spawner.destroy.assert_called_once()
        assert any(
            c.kwargs.get("event_type") == "sandbox_timeout"
            and c.kwargs["details"]["reason"] == "timeout"
            for c in audit.call_args_list
        )


class TestTranscriptSaveDeadline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("module", RUNNER_MODULES)
    async def test_hanging_transcript_store_bounded(
        self, mocker: MockerFixture, module: str
    ) -> None:
        """The transcript-store write runs before destroy, so it's bounded too
        (best-effort: a timeout only logs)."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mod = importlib.import_module(module)
        mocker.patch.object(mod, "SANDBOX_TRANSCRIPT_SAVE_TIMEOUT_SECONDS", 0.05)

        spawner = mocker.AsyncMock()
        spawner.spawn.return_value = "http://pod-1:8080"
        spawner.wait_ready.return_value = True
        spawner.get_query_ssl_context = mocker.Mock(return_value=None)

        run_response = mocker.MagicMock(status_code=200)
        run_response.json.return_value = {"success": True, "output": {}}
        events = mocker.MagicMock(status_code=200, text='{"type":"result","data":{}}\n')
        client = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=run_response),
            get=mocker.AsyncMock(return_value=events),
        )
        _patch_client(mocker, module, client)

        store = mocker.MagicMock()

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        store.save = hang
        step_input = {
            "step": {"name": "s", "prompt": "p", "output_key": "o"},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }
        if module.endswith("step_runner"):
            call = mod.run_step(step_input, spawner=spawner, attempt=1, transcript_store=store)
        else:
            call = mod.run_sandbox_step(step_input, spawner=spawner, transcript_store=store)

        result = await _bounded(call)

        assert result["status"] == "completed"
        spawner.destroy.assert_called_once()


class TestHeartbeatCoversSpawn:
    @staticmethod
    def _step_input() -> dict[str, Any]:
        return {
            "step": {"name": "s", "prompt": "p", "output_key": "o"},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spawn_fails", [False, True])
    async def test_heartbeat_runs_during_spawn_and_is_cancelled(
        self, mocker: MockerFixture, spawn_fails: bool
    ) -> None:
        """A long spawn must not trip the 180s heartbeat timeout, so heartbeats
        start before spawn() and stop on every path."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        from cloud_agents.workflow.executor.temporal import activities

        state = {"running": False, "cancelled": False}

        async def fake_heartbeat_loop(*args, **kwargs):
            state["running"] = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        mocker.patch.object(activities, "_heartbeat_loop", fake_heartbeat_loop)
        seen_during_spawn = {}

        async def spawn(*args, **kwargs):
            await asyncio.sleep(0)
            seen_during_spawn["running"] = state["running"]
            if spawn_fails:
                raise RuntimeError("spawn failed")
            return "http://pod-1:8080"

        spawner = mocker.AsyncMock()
        spawner.spawn.side_effect = spawn
        spawner.wait_ready.return_value = True
        spawner.get_query_ssl_context = mocker.Mock(return_value=None)
        run_response = mocker.MagicMock(status_code=200)
        run_response.json.return_value = {"success": True, "output": {}}
        _patch_client(
            mocker,
            activities.__name__,
            mocker.MagicMock(
                post=mocker.AsyncMock(return_value=run_response),
                get=mocker.AsyncMock(return_value=mocker.MagicMock(status_code=404)),
            ),
        )

        if spawn_fails:
            with pytest.raises(RuntimeError):
                await _bounded(activities.run_sandbox_step(self._step_input(), spawner=spawner))
        else:
            await _bounded(activities.run_sandbox_step(self._step_input(), spawner=spawner))

        assert seen_during_spawn["running"] is True
        assert state["cancelled"] is True
