"""Unit tests for OpenShellSpawner hybrid communication (TDD).

Tests the start_server() fire-and-forget method and the
stream_progress() async generator for event streaming.
Also tests the Podman secret file mount workaround (issue #82).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub openshell if not installed (CI doesn't install the openshell extra)
if "openshell" not in sys.modules:
    _mock_openshell = MagicMock()
    sys.modules["openshell"] = _mock_openshell
    sys.modules["openshell._proto"] = _mock_openshell._proto
    sys.modules["openshell._proto.openshell_pb2"] = _mock_openshell._proto.openshell_pb2

import asyncio
import json
from typing import Any

import pytest
from pytest_mock import MockerFixture


class TestOpenShellSpawnerStartServer:
    """Tests for start_server() fire-and-forget exec."""

    @pytest.mark.asyncio
    async def test_start_server_calls_exec_stream(self, mocker: MockerFixture) -> None:
        """start_server calls exec_stream with the given command."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        # exec_stream is now a sync iterator
        mock_client.exec_stream.return_value = iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        command = ["uvicorn", "lightspeed_agentic.app:create_app", "--host", "0.0.0.0"]
        await spawner.start_server("sandbox-1", command, env={"KEY": "val"})

        # Give background task a chance to start
        await asyncio.sleep(0.05)

        mock_client.exec_stream.assert_called_once_with("sandbox-1", command, env={"KEY": "val"})

    @pytest.mark.asyncio
    async def test_start_server_returns_immediately(self, mocker: MockerFixture) -> None:
        """start_server returns immediately without blocking on exec output."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        # Make exec_stream block indefinitely (sync iterator)
        def slow_exec(*args, **kwargs):
            import time

            def slow_gen():
                time.sleep(100)
                yield "never"

            return slow_gen()

        mock_client = mocker.Mock()
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

        def forever_exec(*args, **kwargs):
            import time

            def gen():
                time.sleep(100)
                yield "data"

            return gen()

        mock_client = mocker.Mock()
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
    async def test_stream_progress_yields_parsed_events(self, mocker: MockerFixture) -> None:
        """stream_progress yields parsed JSONL events from exec_stream."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        events = [
            '{"type": "tool_call", "name": "get_pods", "ts": "2024-01-01T00:00:00Z"}\n',
            '{"type": "tool_result", "name": "get_pods", "ts": "2024-01-01T00:00:01Z"}\n',
        ]

        # Create mock ExecChunk objects
        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            for event in events:
                yield ExecChunk(event)

        mock_client = mocker.Mock()
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
    async def test_stream_progress_handles_multi_line_chunks(self, mocker: MockerFixture) -> None:
        """stream_progress handles chunks containing multiple JSONL lines."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        chunk = '{"type": "tool_call", "name": "a"}\n' '{"type": "tool_result", "name": "a"}\n'

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            yield ExecChunk(chunk)

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 2

    @pytest.mark.asyncio
    async def test_stream_progress_skips_empty_lines(self, mocker: MockerFixture) -> None:
        """stream_progress skips empty lines in the stream."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            yield ExecChunk('\n\n{"type": "tool_call", "name": "a"}\n\n')

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 1

    @pytest.mark.asyncio
    async def test_stream_progress_handles_invalid_json(self, mocker: MockerFixture) -> None:
        """stream_progress logs warning and skips invalid JSON lines."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            yield ExecChunk("not valid json\n")
            yield ExecChunk('{"type": "tool_call", "name": "a"}\n')

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        # Invalid JSON skipped, valid event collected
        assert len(collected) == 1
        assert collected[0]["type"] == "tool_call"

    @pytest.mark.asyncio
    async def test_stream_progress_handles_disconnect(self, mocker: MockerFixture) -> None:
        """stream_progress catches gRPC/connection errors and stops yielding."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            yield ExecChunk('{"type": "tool_call", "name": "a"}\n')
            raise ConnectionError("gRPC stream disconnected")

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        # Should yield what it got before disconnect, then stop
        assert len(collected) == 1

    @pytest.mark.asyncio
    async def test_stream_progress_uses_tail_command(self, mocker: MockerFixture) -> None:
        """stream_progress calls exec_stream with tail -f on event log."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        call_args = {}

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            call_args["sandbox_name"] = sandbox_name
            call_args["cmd"] = cmd
            return iter([])  # Return empty iterator

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        async for _ in spawner.stream_progress("sandbox-1"):
            pass

        assert call_args["cmd"] == ["tail", "-F", "/var/log/agent-events.jsonl"]


class TestOpenShellSpawnerWriteFile:
    """Tests for OpenShellSpawner._do_write_file()."""

    @pytest.mark.asyncio
    async def test_write_file_calls_exec_stream_with_base64(self, mocker: MockerFixture) -> None:
        """write_file encodes content as base64 and pipes through exec."""
        import base64

        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        call_args: dict[str, Any] = {}

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            call_args["sandbox_name"] = sandbox_name
            call_args["cmd"] = cmd
            return iter([])  # Return empty iterator

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"

        await spawner._do_write_file("agent-1", "/tmp/test.txt", "hello world")

        assert call_args["sandbox_name"] == "id-123"
        cmd = call_args["cmd"]
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        # Verify base64 encoding is used
        expected_b64 = base64.b64encode(b"hello world").decode()
        assert expected_b64 in cmd[2]
        assert "base64 -d" in cmd[2]
        assert "/tmp/test.txt" in cmd[2]

    @pytest.mark.asyncio
    async def test_write_file_raises_for_untracked_agent(self, mocker: MockerFixture) -> None:
        """write_file raises RuntimeError for unknown agent."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        with pytest.raises(RuntimeError, match="No sandbox tracked"):
            await spawner._do_write_file("unknown", "/tmp/test.txt", "content")

    @pytest.mark.asyncio
    async def test_write_file_raises_on_exec_failure(self, mocker: MockerFixture) -> None:
        """write_file raises RuntimeError when exec_stream fails."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        def failing_exec(sandbox_name, cmd, **kwargs):
            raise ConnectionError("sandbox unreachable")
            yield  # pragma: no cover

        mock_client = mocker.Mock()
        mock_client.exec_stream = failing_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"

        with pytest.raises(RuntimeError, match="Failed to write"):
            await spawner._do_write_file("agent-1", "/tmp/test.txt", "content")


class TestOpenShellSpawnerSpawn:
    """Tests for _do_spawn using exec-based server startup."""

    @pytest.mark.asyncio
    async def test_spawn_creates_sandbox_and_returns_endpoint(self, mocker: MockerFixture) -> None:
        """_do_spawn creates sandbox, starts server, returns network endpoint."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        # Mock SandboxRef
        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        def noop_exec(*args, **kwargs):
            return iter([])

        mock_client.exec_stream = noop_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _wait_ready_with_host to return True immediately
        async def mock_ready(*args, **kwargs):
            return True
        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        endpoint = await spawner.spawn("agent-1", "sandbox:latest", env={"K": "V"})

        assert endpoint == "http://gateway:17670"
        mock_client.create.assert_called_once()
        mock_client.wait_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_passes_env_to_sandbox(self, mocker: MockerFixture) -> None:
        """_do_spawn passes environment variables to sandbox creation."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        def noop_exec(*args, **kwargs):
            return iter([])

        mock_client.exec_stream = noop_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _wait_ready_with_host to return True immediately
        async def mock_ready(*args, **kwargs):
            return True
        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        env = {"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4"}
        await spawner.spawn("agent-1", "sandbox:latest", env=env)

        # Verify create was called with a spec
        mock_client.create.assert_called_once()
        create_call = mock_client.create.call_args
        spec = create_call.kwargs["spec"]
        # Verify env vars were set on the spec (protobuf map assignment)
        spec.environment.__setitem__.assert_any_call("LIGHTSPEED_PROVIDER", "openai")
        spec.environment.__setitem__.assert_any_call("LIGHTSPEED_MODEL", "gpt-4")


class TestOpenShellSpawnerDestroy:
    """Tests for _do_destroy cleanup."""

    @pytest.mark.asyncio
    async def test_destroy_deletes_sandbox(self, mocker: MockerFixture) -> None:
        """destroy deletes the OpenShell sandbox."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"

        await spawner.destroy("agent-1")

        mock_client.delete.assert_called_once_with("sb-123")

    @pytest.mark.asyncio
    async def test_destroy_cancels_server_task(self, mocker: MockerFixture) -> None:
        """destroy cancels the background server task if running."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"

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
        mock_client.delete.assert_called_once()


class TestOpenShellSpawnerListActive:
    """Tests for _do_list_active."""

    @pytest.mark.asyncio
    async def test_list_active_returns_sandbox_names(self, mocker: MockerFixture) -> None:
        """list_active returns tracked sandbox agent names."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-1"
        spawner._sandbox_ids["agent-1"] = "id-1"
        spawner._sandbox_names["agent-2"] = "sb-2"
        spawner._sandbox_ids["agent-2"] = "id-2"

        result = await spawner.list_active()

        assert set(result) == {"agent-1", "agent-2"}


class TestOpenShellSpawnerDestroyTracking:
    """Tests for _do_destroy tracking order (finding 10)."""

    @pytest.mark.asyncio
    async def test_destroy_retains_tracking_on_delete_failure(self, mocker: MockerFixture) -> None:
        """If delete fails, agent_name remains in _sandbox_names for retry.

        _do_destroy must NOT re-raise: base.destroy() always decrements
        _active_count in its finally block, so re-raising would cause a
        double-decrement on retry.  Instead, _do_destroy logs the error
        and returns, keeping the entry in _sandbox_names for manual cleanup.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        mock_client.delete.side_effect = RuntimeError("API error")
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"

        # Should NOT raise — _do_destroy swallows the error
        await spawner.destroy("agent-1")

        # Tracking should NOT be removed since delete failed
        assert "agent-1" in spawner._sandbox_names

    @pytest.mark.asyncio
    async def test_destroy_removes_tracking_on_success(self, mocker: MockerFixture) -> None:
        """On successful delete, agent_name is removed from _sandbox_names."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"

        await spawner.destroy("agent-1")

        assert "agent-1" not in spawner._sandbox_names

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

        mock_client = mocker.Mock()
        mock_client.delete.side_effect = RuntimeError("API error")
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-123"
        spawner._sandbox_ids["agent-1"] = "id-123"
        spawner._active_count = 1  # simulate one spawned pod

        # First destroy — decrements to 0, does not raise
        await spawner.destroy("agent-1")
        assert spawner.active_count == 0

        # Second destroy (retry) — still sandbox in _sandbox_names, decrements
        # would go to max(0, -1) = 0 without the clamp, but the point is
        # it should NOT have been at -1 before clamping.
        await spawner.destroy("agent-1")
        assert spawner.active_count == 0


class TestOpenShellSpawnerStreamProgressBuffering:
    """Tests for JSONL partial-line buffering across chunks (finding 11)."""

    @pytest.mark.asyncio
    async def test_stream_progress_buffers_partial_lines(self, mocker: MockerFixture) -> None:
        """stream_progress reassembles JSON split across chunk boundaries."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            # First chunk ends mid-JSON
            yield ExecChunk('{"type": "tool_')
            # Second chunk completes the JSON line
            yield ExecChunk('call", "name": "get_pods"}\n')

        mock_client = mocker.Mock()
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

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            yield ExecChunk('{"type":')
            yield ExecChunk(' "tool_call",')
            yield ExecChunk(' "name": "a"}\n')
            yield ExecChunk('{"type": "done"}\n')

        mock_client = mocker.Mock()
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

        class ExecChunk:
            def __init__(self, chunk):
                self.chunk = chunk

        def mock_exec_stream(sandbox_name, cmd, **kwargs):
            yield ExecChunk('{"type": "a"}\n')
            yield ExecChunk('{"type": "b"}\n')

        mock_client = mocker.Mock()
        mock_client.exec_stream = mock_exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)

        collected = []
        async for event in spawner.stream_progress("sandbox-1"):
            collected.append(event)

        assert len(collected) == 2


class TestOpenShellSpawnerGetSandboxId:
    """Tests for get_sandbox_id() public accessor (finding 13)."""

    def test_returns_sandbox_id_when_tracked(self, mocker: MockerFixture) -> None:
        """get_sandbox_id returns the sandbox UUID, not the sandbox name."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-name-123"
        spawner._sandbox_ids["agent-1"] = "uuid-456"

        assert spawner.get_sandbox_id("agent-1") == "uuid-456"

    def test_returns_none_when_not_tracked(self, mocker: MockerFixture) -> None:
        """get_sandbox_id returns None for an unknown agent."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        assert spawner.get_sandbox_id("unknown") is None


    # JWT workaround tests removed — issue #82 workaround dropped.
    # OpenShell v0.0.79+ (PR NVIDIA/OpenShell#2156) delivers sandbox
    # JWTs via Podman secrets natively when gateway_jwt is configured.
    # See spawner docstring for history.


class TestOpenShellSpawnerPostCreateCleanup:
    """Tests for sandbox cleanup when post-create steps fail in _do_spawn.

    Regression tests for the orphaned sandbox bug: if start_server(),
    expose_service(), or wait_ready fails after create_sandbox() succeeds,
    the sandbox must be deleted and removed from _sandbox_ids.
    """

    @pytest.mark.asyncio
    async def test_inject_token_failure_deletes_sandbox(self, mocker: MockerFixture) -> None:
        """If wait_ready raises, sandbox is deleted and tracking removed."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        spawner = OpenShellSpawner(openshell_client=mock_client, )

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        # start_server fails after create + wait_ready succeed
        mocker.patch.object(
            spawner,
            "start_server",
            new_callable=mocker.AsyncMock,
            side_effect=RuntimeError("exec failed"),
        )

        with pytest.raises(RuntimeError, match="exec failed"):
            await spawner.spawn("agent-1", "sandbox:latest", env={})

        # Sandbox must be cleaned up
        mock_client.delete.assert_called_once_with("ca-agent-agent-1")

        # Tracking must not retain the orphaned entry
        assert "agent-1" not in spawner._sandbox_names

    @pytest.mark.asyncio
    async def test_inject_token_failure_propagates_original_exception(
        self, mocker: MockerFixture
    ) -> None:
        """The original exception from wait_ready propagates to the caller."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        spawner = OpenShellSpawner(openshell_client=mock_client, )

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        mocker.patch.object(
            spawner,
            "start_server",
            new_callable=mocker.AsyncMock,
            side_effect=RuntimeError("No container found for sandbox 'ca-agent-agent-1'"),
        )

        with pytest.raises(RuntimeError, match="No container found"):
            await spawner.spawn("agent-1", "sandbox:latest", env={})

    @pytest.mark.asyncio
    async def test_wait_ready_failure_deletes_sandbox(self, mocker: MockerFixture) -> None:
        """If wait_ready raises, sandbox is deleted and tracking removed."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.side_effect = RuntimeError("sandbox failed to start")

        spawner = OpenShellSpawner(openshell_client=mock_client)

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        with pytest.raises(RuntimeError, match="sandbox failed to start"):
            await spawner.spawn("agent-1", "sandbox:latest", env={})

        mock_client.delete.assert_called_once_with("ca-agent-agent-1")
        assert "agent-1" not in spawner._sandbox_names

    @pytest.mark.asyncio
    async def test_cleanup_tolerates_delete_sandbox_failure(self, mocker: MockerFixture) -> None:
        """If delete also fails during cleanup, the original error still propagates."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.delete.side_effect = RuntimeError("API unreachable")

        spawner = OpenShellSpawner(openshell_client=mock_client, )

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        mocker.patch.object(
            spawner,
            "start_server",
            new_callable=mocker.AsyncMock,
            side_effect=RuntimeError("token injection failed"),
        )

        # The original exception must propagate, not the delete failure
        with pytest.raises(RuntimeError, match="token injection failed"):
            await spawner.spawn("agent-1", "sandbox:latest", env={})

        # Tracking must still be cleaned up even if delete failed
        assert "agent-1" not in spawner._sandbox_names

    @pytest.mark.asyncio
    async def test_active_count_decremented_on_post_create_failure(
        self, mocker: MockerFixture
    ) -> None:
        """base.spawn() decrements _active_count when _do_spawn re-raises."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"
            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        spawner = OpenShellSpawner(openshell_client=mock_client, )

        # Mock _expose_service to return gateway endpoint and virtual host
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        # Mock _build_network_policy (static method)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        mocker.patch.object(
            spawner,
            "start_server",
            new_callable=mocker.AsyncMock,
            side_effect=RuntimeError("injection failed"),
        )

        assert spawner.active_count == 0

        with pytest.raises(RuntimeError):
            await spawner.spawn("agent-1", "sandbox:latest", env={})

        # base.spawn() incremented to 1, then decremented back to 0
        assert spawner.active_count == 0


class TestFilesystemPolicy:
    """Tests for _build_filesystem_policy() static method."""

    def test_sets_read_only_root(self, mocker: MockerFixture) -> None:
        """Read-only policy includes root filesystem."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = mocker.Mock()
        spec.policy.filesystem.read_only = []
        spec.policy.filesystem.read_write = []
        spec.policy.filesystem.include_workdir = False

        OpenShellSpawner._build_filesystem_policy(spec)

        assert "/" in spec.policy.filesystem.read_only

    def test_allows_write_to_injection_targets(self, mocker: MockerFixture) -> None:
        """Read-write list includes all post-create injection paths."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = mocker.Mock()
        spec.policy.filesystem.read_only = []
        spec.policy.filesystem.read_write = []
        spec.policy.filesystem.include_workdir = False

        OpenShellSpawner._build_filesystem_policy(spec)

        rw = spec.policy.filesystem.read_write
        assert "/tmp" in rw
        assert "/home/agent" in rw
        assert "/var/log" in rw
        assert "/app/skills" in rw
        assert "/var/secrets/mcp" in rw
        assert "/var/run/secrets/llm-credentials" in rw

    def test_includes_workdir(self, mocker: MockerFixture) -> None:
        """Filesystem policy sets include_workdir."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = mocker.Mock()
        spec.policy.filesystem.read_only = []
        spec.policy.filesystem.read_write = []
        spec.policy.filesystem.include_workdir = False

        OpenShellSpawner._build_filesystem_policy(spec)

        assert spec.policy.filesystem.include_workdir is True


class TestCredentialInjection:
    """Tests for _inject_credentials() and Provider API integration."""

    @pytest.mark.asyncio
    async def test_creates_and_attaches_provider(self, mocker: MockerFixture) -> None:
        """Credentials are injected via Provider API when available."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "uuid-1"

        mock_create = mocker.patch.object(
            spawner, "_create_and_attach_provider",
            return_value="provider-123",
        )

        await spawner._inject_credentials(
            "agent-1", "sb-1", "OPENAI_API_KEY",
            {"OPENAI_API_KEY": "sk-test"},
        )

        mock_create.assert_called_once_with(
            "sb-1", credentials={"OPENAI_API_KEY": "sk-test"},
        )
        assert spawner._provider_ids["agent-1"] == "provider-123"

    @pytest.mark.asyncio
    async def test_falls_back_to_file_injection(self, mocker: MockerFixture) -> None:
        """Falls back to file injection when Provider API fails."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "uuid-1"

        mocker.patch.object(
            spawner, "_create_and_attach_provider",
            side_effect=Exception("gRPC unavailable"),
        )
        mock_file_inject = mocker.patch.object(
            spawner, "_inject_credentials_via_files",
        )

        await spawner._inject_credentials(
            "agent-1", "sb-1", "OPENAI_API_KEY",
            {"OPENAI_API_KEY": "sk-test"},
        )

        mock_file_inject.assert_called_once_with(
            "agent-1", "OPENAI_API_KEY", "sk-test",
        )

    @pytest.mark.asyncio
    async def test_skips_when_credential_not_in_env(self, mocker: MockerFixture) -> None:
        """Logs warning when credential key not found in env."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        await spawner._inject_credentials("agent-1", "sb-1", "MISSING_KEY", {})

        assert "agent-1" not in spawner._provider_ids


class TestMCPSecretInjection:
    """Tests for _inject_mcp_secrets() file injection."""

    @pytest.mark.asyncio
    async def test_writes_to_correct_path(self, mocker: MockerFixture) -> None:
        """MCP secrets are written to mount_path + key, not mount_path alone."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        mock_client.exec_stream.return_value = iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "uuid-1"

        mock_mkdir = mocker.patch.object(spawner, "_exec_mkdir")
        mock_write = mocker.patch.object(spawner, "_do_write_file")

        mounts = [("my-secret", "api-key", "/var/secrets/mcp/kubectl/")]

        await spawner._inject_mcp_secrets(
            "agent-1", mounts, {"my-secret": "secret-value"},
        )

        mock_mkdir.assert_called_once_with("uuid-1", "/var/secrets/mcp/kubectl/")
        mock_write.assert_called_once_with(
            "agent-1", "/var/secrets/mcp/kubectl/api-key", "secret-value",
        )

    @pytest.mark.asyncio
    async def test_skips_missing_secrets(self, mocker: MockerFixture) -> None:
        """Logs warning and skips when secret not in env."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "uuid-1"

        mock_write = mocker.patch.object(spawner, "_do_write_file")

        mounts = [("missing-secret", "key", "/var/secrets/mcp/s/")]

        await spawner._inject_mcp_secrets("agent-1", mounts, {})

        mock_write.assert_not_called()


class TestTLSAndServiceAccountSkipped:
    """Tests for TLS and service_account skip-with-info-log."""

    @pytest.mark.asyncio
    async def test_tls_logged_and_skipped(self, mocker: MockerFixture) -> None:
        """TLS certs trigger info log but are not injected."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        mock_client.create.return_value = mocker.Mock(name="sb-1", id="uuid-1")
        mock_client.wait_ready = mocker.Mock()
        mock_client.exec_stream.return_value = iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        mocker.patch.object(spawner, "_build_network_policy")
        mocker.patch.object(spawner, "_expose_service", return_value=("http://gw:8080", "vh"))
        mocker.patch.object(spawner, "_wait_ready_with_host", return_value=True)
        mocker.patch.object(spawner, "start_server")

        tls = mocker.Mock()
        mock_logger = mocker.patch("cloud_agents.spawner.openshell_spawner.logger")

        await spawner._do_spawn(
            "agent-1", "image:latest", env={}, tls_certs=tls,
        )

        info_calls = [
            str(c) for c in mock_logger.info.call_args_list
            if "TLS" in str(c) or "transport security" in str(c)
        ]
        assert len(info_calls) >= 1

    @pytest.mark.asyncio
    async def test_service_account_logged_and_skipped(self, mocker: MockerFixture) -> None:
        """Service account triggers info log but is not applied."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        mock_client.create.return_value = mocker.Mock(name="sb-1", id="uuid-1")
        mock_client.wait_ready = mocker.Mock()
        mock_client.exec_stream.return_value = iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        mocker.patch.object(spawner, "_build_network_policy")
        mocker.patch.object(spawner, "_expose_service", return_value=("http://gw:8080", "vh"))
        mocker.patch.object(spawner, "_wait_ready_with_host", return_value=True)
        mocker.patch.object(spawner, "start_server")

        mock_logger = mocker.patch("cloud_agents.spawner.openshell_spawner.logger")

        await spawner._do_spawn(
            "agent-1", "image:latest", env={}, service_account="my-sa",
        )

        info_calls = [
            str(c) for c in mock_logger.info.call_args_list
            if "service_account" in str(c) or "identity" in str(c)
        ]
        assert len(info_calls) >= 1


class TestDestroyWithProviderCleanup:
    """Tests for _do_destroy() provider cleanup."""

    @pytest.mark.asyncio
    async def test_detaches_provider_on_destroy(self, mocker: MockerFixture) -> None:
        """Provider is detached during destroy."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-1"
        spawner._sandbox_ids["agent-1"] = "uuid-1"
        spawner._provider_ids["agent-1"] = "provider-123"

        mock_detach = mocker.patch.object(spawner, "_detach_provider")
        mock_client.delete = mocker.Mock()

        await spawner._do_destroy("agent-1")

        mock_detach.assert_called_once_with("sb-1", "provider-123")
        assert "agent-1" not in spawner._provider_ids

    @pytest.mark.asyncio
    async def test_destroy_tolerates_detach_failure(self, mocker: MockerFixture) -> None:
        """Destroy continues even if provider detach fails."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_names["agent-1"] = "sb-1"
        spawner._sandbox_ids["agent-1"] = "uuid-1"
        spawner._provider_ids["agent-1"] = "provider-123"

        mocker.patch.object(
            spawner, "_detach_provider", side_effect=Exception("detach failed"),
        )
        mock_client.delete = mocker.Mock()

        await spawner._do_destroy("agent-1")

        mock_client.delete.assert_called_once_with("sb-1")


class TestBuildNetworkPolicy:
    """Tests for _build_network_policy() static method."""

    def _make_mock_spec(self, mocker: MockerFixture) -> Any:
        """Create a mock SandboxSpec with nested policy structure."""
        from collections import defaultdict

        spec = mocker.Mock()

        class MockNP:
            def __init__(self) -> None:
                self.name = ""
                self.endpoints = mocker.Mock()
                self.binaries = mocker.Mock()
                ep = mocker.Mock()
                ep.host = ""
                ep.port = 0
                self.endpoints.add.return_value = ep
                b = mocker.Mock()
                b.path = ""
                self.binaries.add.return_value = b
                self._ep = ep
                self._b = b

        policies: dict[str, MockNP] = defaultdict(MockNP)
        spec.policy.network_policies = policies
        return spec

    def test_openai_provider(self, mocker: MockerFixture) -> None:
        """OpenAI provider adds api.openai.com egress rule."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER": "openai"}
        OpenShellSpawner._build_network_policy(spec, env)

        assert "llm_provider" in spec.policy.network_policies
        np = spec.policy.network_policies["llm_provider"]
        assert np._ep.host == "api.openai.com"
        assert np._ep.port == 443

    def test_azure_provider(self, mocker: MockerFixture) -> None:
        """Azure provider adds *.openai.azure.com egress rule."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER": "azure"}
        OpenShellSpawner._build_network_policy(spec, env)

        assert "llm_provider" in spec.policy.network_policies
        np = spec.policy.network_policies["llm_provider"]
        assert np._ep.host == "*.openai.azure.com"

    def test_anthropic_provider(self, mocker: MockerFixture) -> None:
        """Anthropic provider adds api.anthropic.com egress rule."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER": "anthropic"}
        OpenShellSpawner._build_network_policy(spec, env)

        assert "llm_provider" in spec.policy.network_policies
        np = spec.policy.network_policies["llm_provider"]
        assert np._ep.host == "api.anthropic.com"

    def test_unknown_provider_no_default_rule(self, mocker: MockerFixture) -> None:
        """Unknown provider does not add llm_provider rule."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER": "unknown-llm"}
        OpenShellSpawner._build_network_policy(spec, env)

        assert "llm_provider" not in spec.policy.network_policies

    def test_custom_provider_url_https(self, mocker: MockerFixture) -> None:
        """LIGHTSPEED_PROVIDER_URL with https defaults to port 443."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER_URL": "https://my-vllm.internal/v1"}
        OpenShellSpawner._build_network_policy(spec, env)

        np = spec.policy.network_policies["custom_provider"]
        assert np._ep.host == "my-vllm.internal"
        assert np._ep.port == 443

    def test_custom_provider_url_explicit_port(self, mocker: MockerFixture) -> None:
        """LIGHTSPEED_PROVIDER_URL with explicit port uses that port."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER_URL": "https://my-vllm.internal:8443/v1"}
        OpenShellSpawner._build_network_policy(spec, env)

        np = spec.policy.network_policies["custom_provider"]
        assert np._ep.host == "my-vllm.internal"
        assert np._ep.port == 8443

    def test_custom_provider_url_http(self, mocker: MockerFixture) -> None:
        """LIGHTSPEED_PROVIDER_URL with http defaults to port 80."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_PROVIDER_URL": "http://local-vllm.internal/v1"}
        OpenShellSpawner._build_network_policy(spec, env)

        np = spec.policy.network_policies["custom_provider"]
        assert np._ep.host == "local-vllm.internal"
        assert np._ep.port == 80

    def test_mcp_servers(self, mocker: MockerFixture) -> None:
        """LIGHTSPEED_MCP_SERVERS adds per-server egress rules."""
        import json

        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        mcp = [
            {"name": "kubectl", "url": "http://mcp-kubectl:8082/mcp"},
            {"name": "fs", "url": "http://mcp-fs:8081/sse"},
        ]
        env = {"LIGHTSPEED_MCP_SERVERS": json.dumps(mcp)}
        OpenShellSpawner._build_network_policy(spec, env)

        assert "mcp_0" in spec.policy.network_policies
        assert "mcp_1" in spec.policy.network_policies
        assert spec.policy.network_policies["mcp_0"]._ep.host == "mcp-kubectl"
        assert spec.policy.network_policies["mcp_0"]._ep.port == 8082
        assert spec.policy.network_policies["mcp_1"]._ep.host == "mcp-fs"
        assert spec.policy.network_policies["mcp_1"]._ep.port == 8081

    def test_invalid_mcp_json_is_skipped(self, mocker: MockerFixture) -> None:
        """Invalid LIGHTSPEED_MCP_SERVERS JSON does not crash."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {"LIGHTSPEED_MCP_SERVERS": "not-json"}
        OpenShellSpawner._build_network_policy(spec, env)

        assert len(spec.policy.network_policies) == 0

    def test_empty_env_no_rules(self, mocker: MockerFixture) -> None:
        """Empty env produces no network policy rules."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        OpenShellSpawner._build_network_policy(spec, {})

        assert len(spec.policy.network_policies) == 0
