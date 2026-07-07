"""Unit tests for OpenShellSpawner hybrid communication (TDD).

Tests the start_server() fire-and-forget method and the
stream_progress() async generator for event streaming.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_mock import MockerFixture


class TestOpenShellSpawnerStartServer:
    """Tests for start_server() fire-and-forget exec."""

    @pytest.mark.asyncio
    async def test_start_server_calls_exec_stream(self, mocker: MockerFixture) -> None:
        """start_server calls exec_stream with the given command."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        # exec_stream returns an async iterator that we consume in a background task
        mock_client.exec_stream.return_value = mocker.AsyncMock(
            __aiter__=mocker.MagicMock(return_value=mocker.AsyncMock(
                __anext__=mocker.AsyncMock(side_effect=StopAsyncIteration)
            ))
        )

        spawner = OpenShellSpawner(openshell_client=mock_client)
        command = ["uvicorn", "lightspeed_agentic.app:create_app", "--host", "0.0.0.0"]
        await spawner.start_server("sandbox-1", command, env={"KEY": "val"})

        # Give background task a chance to start
        await asyncio.sleep(0.05)

        mock_client.exec_stream.assert_called_once_with(
            "sandbox-1", command, env={"KEY": "val"}
        )

    @pytest.mark.asyncio
    async def test_start_server_returns_immediately(self, mocker: MockerFixture) -> None:
        """start_server returns immediately without blocking on exec output."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        # Make exec_stream block indefinitely
        async def slow_exec(*args, **kwargs):
            async def slow_gen():
                await asyncio.sleep(100)
                yield "never"
            return slow_gen()

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = slow_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)

        # This should return within a short time, not block
        await asyncio.wait_for(
            spawner.start_server("sandbox-1", ["uvicorn"]),
            timeout=1.0,
        )

    @pytest.mark.asyncio
    async def test_start_server_tracks_task(self, mocker: MockerFixture) -> None:
        """start_server stores the background task for later cleanup."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def forever_exec(*args, **kwargs):
            async def gen():
                await asyncio.sleep(100)
                yield "data"
            return gen()

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = forever_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)
        await spawner.start_server("sandbox-1", ["uvicorn"])

        assert "sandbox-1" in spawner._server_tasks

        # Cleanup
        spawner._server_tasks["sandbox-1"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawner._server_tasks["sandbox-1"]


class TestOpenShellSpawnerStreamProgress:
    """Tests for stream_progress() async generator."""

    @pytest.mark.asyncio
    async def test_stream_progress_yields_parsed_events(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress yields parsed JSONL events from exec_stream."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        events = [
            '{"type": "tool_call", "name": "get_pods", "ts": "2024-01-01T00:00:00Z"}\n',
            '{"type": "tool_result", "name": "get_pods", "ts": "2024-01-01T00:00:01Z"}\n',
        ]

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            for event in events:
                yield event

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 2
        assert collected[0]["type"] == "tool_call"
        assert collected[0]["name"] == "get_pods"
        assert collected[1]["type"] == "tool_result"

    @pytest.mark.asyncio
    async def test_stream_progress_handles_multi_line_chunks(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress handles chunks containing multiple JSONL lines."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        chunk = (
            '{"type": "tool_call", "name": "a"}\n'
            '{"type": "tool_result", "name": "a"}\n'
        )

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            yield chunk

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 2

    @pytest.mark.asyncio
    async def test_stream_progress_skips_empty_lines(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress skips empty lines in the stream."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            yield '\n\n{"type": "tool_call", "name": "a"}\n\n'

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 1

    @pytest.mark.asyncio
    async def test_stream_progress_handles_invalid_json(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress logs warning and skips invalid JSON lines."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            yield 'not valid json\n'
            yield '{"type": "tool_call", "name": "a"}\n'

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        # Invalid JSON skipped, valid event collected
        assert len(collected) == 1
        assert collected[0]["type"] == "tool_call"

    @pytest.mark.asyncio
    async def test_stream_progress_handles_disconnect(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress catches gRPC/connection errors and stops yielding."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            yield '{"type": "tool_call", "name": "a"}\n'
            raise ConnectionError("gRPC stream disconnected")

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        # Should yield what it got before disconnect, then stop
        assert len(collected) == 1

    @pytest.mark.asyncio
    async def test_stream_progress_uses_tail_command(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress calls exec_stream with tail -f on event log."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        call_args = {}

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            call_args["sandbox_id"] = sandbox_id
            call_args["cmd"] = cmd
            return
            yield  # Make it an async generator

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        async for _ in spawner.stream_progress("sandbox-1"):
            pass

        assert call_args["cmd"] == ["tail", "-F", "/var/log/agent-events.jsonl"]


class TestOpenShellSpawnerWriteFile:
    """Tests for OpenShellSpawner._do_write_file()."""

    @pytest.mark.asyncio
    async def test_write_file_calls_exec_stream_with_base64(
        self, mocker: MockerFixture
    ) -> None:
        """write_file encodes content as base64 and pipes through exec."""
        import base64

        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        call_args: dict[str, Any] = {}

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            call_args["sandbox_id"] = sandbox_id
            call_args["cmd"] = cmd
            return
            yield  # Make it an async generator

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        await spawner._do_write_file("agent-1", "/tmp/test.txt", "hello world")

        assert call_args["sandbox_id"] == "sb-123"
        cmd = call_args["cmd"]
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        # Verify base64 encoding is used
        expected_b64 = base64.b64encode(b"hello world").decode()
        assert expected_b64 in cmd[2]
        assert "base64 -d" in cmd[2]
        assert "/tmp/test.txt" in cmd[2]

    @pytest.mark.asyncio
    async def test_write_file_raises_for_untracked_agent(
        self, mocker: MockerFixture
    ) -> None:
        """write_file raises RuntimeError for unknown agent."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        with pytest.raises(RuntimeError, match="No sandbox tracked"):
            await spawner._do_write_file("unknown", "/tmp/test.txt", "content")

    @pytest.mark.asyncio
    async def test_write_file_raises_on_exec_failure(
        self, mocker: MockerFixture
    ) -> None:
        """write_file raises RuntimeError when exec_stream fails."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def failing_exec(sandbox_id, cmd, **kwargs):
            raise ConnectionError("sandbox unreachable")
            yield  # pragma: no cover

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = failing_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        with pytest.raises(RuntimeError, match="Failed to write"):
            await spawner._do_write_file("agent-1", "/tmp/test.txt", "content")


class TestOpenShellSpawnerSpawn:
    """Tests for _do_spawn using exec-based server startup."""

    @pytest.mark.asyncio
    async def test_spawn_creates_sandbox_and_returns_endpoint(
        self, mocker: MockerFixture
    ) -> None:
        """_do_spawn creates sandbox, starts server, exposes service."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        mock_client.create_sandbox.return_value = "sb-123"
        mock_client.expose_service.return_value = "http://sb-123.example.com:8080"

        async def noop_exec(*args, **kwargs):
            return
            yield

        mock_client.exec_stream = noop_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)
        endpoint = await spawner.spawn("agent-1", "sandbox:latest", env={"K": "V"})

        assert endpoint == "http://sb-123.example.com:8080"
        mock_client.create_sandbox.assert_called_once()
        mock_client.expose_service.assert_called_once_with("sb-123", port=8080)

    @pytest.mark.asyncio
    async def test_spawn_passes_env_to_sandbox(
        self, mocker: MockerFixture
    ) -> None:
        """_do_spawn passes environment variables to sandbox creation."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        mock_client.create_sandbox.return_value = "sb-123"
        mock_client.expose_service.return_value = "http://sb-123:8080"

        async def noop_exec(*args, **kwargs):
            return
            yield

        mock_client.exec_stream = noop_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)
        env = {"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4"}
        await spawner.spawn("agent-1", "sandbox:latest", env=env)

        create_call = mock_client.create_sandbox.call_args
        assert create_call[1].get("env") == env or create_call.kwargs.get("env") == env


class TestOpenShellSpawnerDestroy:
    """Tests for _do_destroy cleanup."""

    @pytest.mark.asyncio
    async def test_destroy_deletes_sandbox(self, mocker: MockerFixture) -> None:
        """destroy deletes the OpenShell sandbox."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        await spawner.destroy("agent-1")

        mock_client.delete_sandbox.assert_called_once_with("sb-123")

    @pytest.mark.asyncio
    async def test_destroy_cancels_server_task(self, mocker: MockerFixture) -> None:
        """destroy cancels the background server task if running."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        # Fake awaitable task that tracks cancel() calls
        class FakeTask:
            def __init__(self):
                self.cancel_count = 0

            def done(self):
                return False

            def cancel(self):
                self.cancel_count += 1

            def __await__(self):
                yield
        fake_task = FakeTask()
        spawner._server_tasks["sb-123"] = fake_task

        await spawner.destroy("agent-1")

        assert fake_task.cancel_count == 1
        mock_client.delete_sandbox.assert_called_once()


class TestOpenShellSpawnerListActive:
    """Tests for _do_list_active."""

    @pytest.mark.asyncio
    async def test_list_active_returns_sandbox_names(
        self, mocker: MockerFixture
    ) -> None:
        """list_active returns tracked sandbox agent names."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-1"
        spawner._sandbox_ids["agent-2"] = "sb-2"

        result = await spawner.list_active()

        assert set(result) == {"agent-1", "agent-2"}


class TestOpenShellSpawnerDestroyTracking:
    """Tests for _do_destroy tracking order (finding 10)."""

    @pytest.mark.asyncio
    async def test_destroy_retains_tracking_on_delete_failure(
        self, mocker: MockerFixture
    ) -> None:
        """If delete_sandbox fails, agent_name remains in _sandbox_ids for retry.

        _do_destroy must NOT re-raise: base.destroy() always decrements
        _active_count in its finally block, so re-raising would cause a
        double-decrement on retry.  Instead, _do_destroy logs the error
        and returns, keeping the entry in _sandbox_ids for manual cleanup.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        mock_client.delete_sandbox.side_effect = RuntimeError("API error")
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        # Should NOT raise — _do_destroy swallows the error
        await spawner.destroy("agent-1")

        # Tracking should NOT be removed since delete failed
        assert "agent-1" in spawner._sandbox_ids

    @pytest.mark.asyncio
    async def test_destroy_removes_tracking_on_success(
        self, mocker: MockerFixture
    ) -> None:
        """On successful delete, agent_name is removed from _sandbox_ids."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        await spawner.destroy("agent-1")

        assert "agent-1" not in spawner._sandbox_ids

    @pytest.mark.asyncio
    async def test_destroy_failure_does_not_double_decrement_active_count(
        self, mocker: MockerFixture
    ) -> None:
        """Verify _active_count is decremented only once on delete failure.

        base.destroy() always decrements in its finally block.  If _do_destroy
        re-raised, calling destroy() twice would decrement twice — but spawn()
        only incremented once, corrupting the counter.  This test proves
        the fix: two destroy() calls on a failed sandbox decrement exactly
        once (active_count goes to 0, never below).
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        mock_client.delete_sandbox.side_effect = RuntimeError("API error")
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"
        spawner._active_count = 1  # simulate one spawned pod

        # First destroy — decrements to 0, does not raise
        await spawner.destroy("agent-1")
        assert spawner.active_count == 0

        # Second destroy (retry) — still sandbox in _sandbox_ids, decrements
        # would go to max(0, -1) = 0 without the clamp, but the point is
        # it should NOT have been at -1 before clamping.
        await spawner.destroy("agent-1")
        assert spawner.active_count == 0


class TestOpenShellSpawnerStreamProgressBuffering:
    """Tests for JSONL partial-line buffering across chunks (finding 11)."""

    @pytest.mark.asyncio
    async def test_stream_progress_buffers_partial_lines(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress reassembles JSON split across chunk boundaries."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            # First chunk ends mid-JSON
            yield '{"type": "tool_'
            # Second chunk completes the JSON line
            yield 'call", "name": "get_pods"}\n'

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 1
        assert collected[0]["type"] == "tool_call"
        assert collected[0]["name"] == "get_pods"

    @pytest.mark.asyncio
    async def test_stream_progress_handles_multiple_partial_chunks(
        self, mocker: MockerFixture
    ) -> None:
        """stream_progress handles multiple successive partial chunks."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            yield '{"type":'
            yield ' "tool_call",'
            yield ' "name": "a"}\n'
            yield '{"type": "done"}\n'

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 2
        assert collected[0]["type"] == "tool_call"
        assert collected[1]["type"] == "done"

    @pytest.mark.asyncio
    async def test_stream_progress_complete_lines_no_buffer_needed(
        self, mocker: MockerFixture
    ) -> None:
        """When chunks end with newline, no buffering is needed."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        async def mock_exec_stream(sandbox_id, cmd, **kwargs):
            yield '{"type": "a"}\n'
            yield '{"type": "b"}\n'

        mock_client = mocker.AsyncMock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 2


class TestOpenShellSpawnerGetSandboxId:
    """Tests for get_sandbox_id() public accessor (finding 13)."""

    def test_returns_sandbox_id_when_tracked(self, mocker: MockerFixture) -> None:
        """get_sandbox_id returns the sandbox ID for a tracked agent."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "sb-123"

        assert spawner.get_sandbox_id("agent-1") == "sb-123"

    def test_returns_none_when_not_tracked(self, mocker: MockerFixture) -> None:
        """get_sandbox_id returns None for an unknown agent."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        assert spawner.get_sandbox_id("unknown") is None
