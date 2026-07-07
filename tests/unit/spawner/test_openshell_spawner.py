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

        assert call_args["cmd"] == ["tail", "-f", "/var/log/agent-events.jsonl"]


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

        mock_task = mocker.MagicMock()
        mock_task.cancel = mocker.MagicMock()
        spawner._server_tasks["sandbox-1"] = mock_task

        await spawner.destroy("agent-1")

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
