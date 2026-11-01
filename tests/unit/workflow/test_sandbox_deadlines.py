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
