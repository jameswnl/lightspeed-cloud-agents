"""Sandbox I/O stays inside its advertised budget even when cleanup stalls.

Each phase runs as a task waited on for at most its deadline, then at most a
short cancel grace; a task still running after that is abandoned (never
awaited again) and the step doesn't go on to touch the sandbox. Parameterized
over the local (step_runner.run_step) and Temporal (run_sandbox_step) engines.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import time
from typing import Any

import pytest
from pytest_mock import MockerFixture

GRACE = 0.1
RUN_TIMEOUT = 0.2
STUBBORN = 0.6  # how long "uncooperative" code ignores cancellation

ENGINES = ["local", "temporal"]


def _module(engine: str):
    return importlib.import_module(
        "cloud_agents.workflow.core.step_runner"
        if engine == "local"
        else "cloud_agents.workflow.executor.temporal.activities"
    )


async def _stubborn_sleep(seconds: float) -> None:
    """Sleep for `seconds`, ignoring cancellation meanwhile."""
    end = time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass


class _FakeClientCM:
    """httpx.AsyncClient stand-in whose __aexit__ can stall."""

    def __init__(self, client: Any, aexit=None) -> None:
        self.client = client
        self.aexit = aexit

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        if self.aexit is not None:
            await self.aexit()
        return False


@pytest.fixture(autouse=True)
def _short_grace(mocker):
    from cloud_agents.spawner import lifetime

    mocker.patch.object(lifetime, "SANDBOX_CANCEL_GRACE_SECONDS", GRACE, create=True)
    mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)


def _spawner(mocker):
    spawner = mocker.AsyncMock()
    spawner.spawn.return_value = "http://pod-1:8080"
    spawner.wait_ready.return_value = True
    spawner.get_query_ssl_context = mocker.Mock(return_value=None)
    return spawner


def _ok_response(mocker):
    response = mocker.MagicMock(status_code=200)
    response.json.return_value = {"success": True, "output": {}}
    return response


def _step_input(run_timeout: float | None = RUN_TIMEOUT) -> dict[str, Any]:
    step: dict[str, Any] = {"name": "s", "prompt": "p", "output_key": "o"}
    if run_timeout is not None:
        step["permissions"] = {"timeout_seconds": run_timeout}
    return {
        "step": step,
        "workflow_id": "wf-1",
        "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        "sandbox_image": "sandbox:latest",
        "context": {},
    }


def _run(engine: str, mod, step_input, spawner, **kwargs):
    if engine == "local":
        return mod.run_step(step_input, spawner=spawner, attempt=1, **kwargs)
    return mod.run_sandbox_step(step_input, spawner=spawner, **kwargs)


async def _timed(coro) -> tuple[float, Any, BaseException | None]:
    start = time.monotonic()
    try:
        result, error = await asyncio.wait_for(coro, timeout=10), None
    except Exception as exc:  # noqa: BLE001 - the error is the assertion subject
        result, error = None, exc
    return time.monotonic() - start, result, error


class TestRunCallBounded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ENGINES)
    async def test_post_times_out_then_close_stalls(self, mocker: MockerFixture, engine) -> None:
        """(a) The POST overruns its deadline and the client close then ignores
        cancellation: the step still ends within run timeout + grace, and no
        transcript access happens afterwards."""
        mod = _module(engine)
        get = mocker.AsyncMock()

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        client = mocker.MagicMock(post=hang, get=get)
        mocker.patch(
            f"{mod.__name__}.httpx.AsyncClient",
            side_effect=lambda **kw: _FakeClientCM(client, lambda: _stubborn_sleep(STUBBORN)),
        )

        elapsed, _, error = await _timed(_run(engine, mod, _step_input(), _spawner(mocker)))

        assert isinstance(error, (TimeoutError, RuntimeError))
        assert "timed out" in str(error)
        assert elapsed < RUN_TIMEOUT + GRACE + 0.3
        get.assert_not_called()
        await asyncio.sleep(STUBBORN)  # let the abandoned close finish

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ENGINES)
    async def test_post_succeeds_then_close_stalls(self, mocker: MockerFixture, engine) -> None:
        """The run deadline covers the client's __aexit__ too."""
        mod = _module(engine)
        get = mocker.AsyncMock()
        client = mocker.MagicMock(post=mocker.AsyncMock(return_value=_ok_response(mocker)), get=get)

        async def stall():
            await asyncio.sleep(3600)

        mocker.patch(
            f"{mod.__name__}.httpx.AsyncClient",
            side_effect=lambda **kw: _FakeClientCM(client, stall),
        )

        elapsed, _, error = await _timed(_run(engine, mod, _step_input(), _spawner(mocker)))

        assert error is not None and "timed out" in str(error)
        assert elapsed < RUN_TIMEOUT + GRACE + 0.3
        get.assert_not_called()


class TestProgressCleanupBounded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ENGINES)
    async def test_progress_task_ignoring_cancellation_is_abandoned(
        self, mocker: MockerFixture, engine
    ) -> None:
        """(b) A progress stream that ignores cancellation is abandoned within
        the cancel grace; the step still completes."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mod = _module(engine)
        spawner = OpenShellSpawner(openshell_client=mocker.AsyncMock())
        mocker.patch.object(spawner, "spawn", return_value="http://pod-1:8080")
        mocker.patch.object(spawner, "wait_ready", return_value=True)
        mocker.patch.object(spawner, "destroy", return_value=None)
        pod = mod.compute_pod_name("wf-1", "s", 1)
        spawner._sandbox_names[pod] = "sb-1"
        spawner._sandbox_ids[pod] = "sb-1"

        state = {"cancelled": False}

        async def stubborn_progress(*args, **kwargs):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] = True
                await _stubborn_sleep(STUBBORN)

        loop_name = "_stream_progress" if engine == "local" else "_progress_streaming_loop"
        mocker.patch.object(mod, loop_name, stubborn_progress)

        async def post(*args, **kwargs):
            await asyncio.sleep(0.02)
            return _ok_response(mocker)

        client = mocker.MagicMock(
            post=post, get=mocker.AsyncMock(return_value=mocker.MagicMock(status_code=404))
        )
        mocker.patch(
            f"{mod.__name__}.httpx.AsyncClient", side_effect=lambda **kw: _FakeClientCM(client)
        )
        mocker.patch(f"{mod.__name__}.activity.heartbeat") if engine == "temporal" else None
        kwargs = {"on_progress": lambda event: None} if engine == "local" else {}

        elapsed, result, error = await _timed(
            _run(engine, mod, _step_input(run_timeout=None), spawner, **kwargs)
        )

        assert error is None, error
        assert result["status"] == "completed"
        assert state["cancelled"] is True
        assert elapsed < STUBBORN
        await asyncio.sleep(STUBBORN)


class TestTranscriptFetchBounded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ENGINES)
    async def test_transcript_client_close_stalls(self, mocker: MockerFixture, engine) -> None:
        """(c) The transcript client's __aexit__ ignores cancellation: the fetch
        is abandoned within transcript timeout + grace and the step completes
        with an empty transcript."""
        mod = _module(engine)
        mocker.patch.object(mod, "SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS", 0.1)
        run_client = mocker.MagicMock(post=mocker.AsyncMock(return_value=_ok_response(mocker)))
        events = mocker.MagicMock(status_code=200, text='{"type":"result","data":{}}\n')
        transcript_client = mocker.MagicMock(get=mocker.AsyncMock(return_value=events))
        clients = iter(
            [
                _FakeClientCM(run_client),
                _FakeClientCM(transcript_client, lambda: _stubborn_sleep(STUBBORN)),
            ]
        )
        mocker.patch(f"{mod.__name__}.httpx.AsyncClient", side_effect=lambda **kw: next(clients))

        elapsed, result, error = await _timed(
            _run(engine, mod, _step_input(run_timeout=None), _spawner(mocker))
        )

        assert error is None, error
        assert result["status"] == "completed"
        assert result["transcript"]["events"] == []
        assert elapsed < 0.1 + GRACE + 0.3
        await asyncio.sleep(STUBBORN)


class TestAwaitWithin:
    """The helper itself: bounded wait, bounded cancel, abandon + log."""

    @pytest.mark.asyncio
    async def test_returns_result(self) -> None:
        from cloud_agents.spawner.lifetime import await_within

        async def ok():
            return 42

        assert await await_within(ok(), 1, what="x") == 42

    @pytest.mark.asyncio
    async def test_propagates_errors(self) -> None:
        from cloud_agents.spawner.lifetime import await_within

        async def boom():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await await_within(boom(), 1, what="x")

    @pytest.mark.asyncio
    async def test_uncooperative_task_abandoned_and_logged(self, caplog) -> None:
        from cloud_agents.spawner.lifetime import await_within

        start = time.monotonic()
        with caplog.at_level("WARNING"), pytest.raises(TimeoutError, match="thing timed out"):
            await await_within(_stubborn_sleep(STUBBORN), 0.05, what="thing")

        assert time.monotonic() - start < 0.05 + GRACE + 0.2
        assert "abandon" in caplog.text
        await asyncio.sleep(STUBBORN)

    @pytest.mark.asyncio
    async def test_caller_cancelled_during_grace(self, mocker, caplog) -> None:
        """If the caller is cancelled again while waiting out the cancel grace,
        CancelledError propagates, the abandonment is logged, and the work's
        eventual exception is still consumed (no "never retrieved" report)."""
        import gc

        from cloud_agents.spawner import lifetime
        from cloud_agents.spawner.lifetime import await_within

        mocker.patch.object(lifetime, "SANDBOX_CANCEL_GRACE_SECONDS", 5)
        loop = asyncio.get_running_loop()
        reported: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: reported.append(ctx))

        async def stubborn_then_fail():
            await _stubborn_sleep(0.4)
            raise ValueError("late failure")

        before = asyncio.all_tasks()
        caller = asyncio.create_task(await_within(stubborn_then_fail(), 0.05, what="thing"))
        await asyncio.sleep(0.15)  # past the deadline, inside the grace
        (inner,) = asyncio.all_tasks() - before - {caller}
        try:
            with caplog.at_level("WARNING"):
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            while not inner.done():
                await asyncio.sleep(0.05)
            del inner
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous)

        assert not any("never retrieved" in str(c.get("message")) for c in reported), reported
        assert "abandon" in caplog.text
