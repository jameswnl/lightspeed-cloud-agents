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
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import pytest
from pytest_mock import MockerFixture


@contextmanager
def _real_openshell_modules() -> "Iterator[None]":
    """Temporarily swap in the real `openshell` package over the CI test-stub MagicMock.

    This test file stubs `openshell` with a MagicMock at import time when the
    real package isn't installed (CI doesn't install the openshell extra).
    When it IS installed (e.g. local dev), swap the stub out for the real
    modules for the duration of a test that needs to construct real protobuf
    objects.

    Restores the FULL sys.modules snapshot afterward, not just the
    originally-mocked keys -- a naive "pop these keys, then restore only
    those same keys" approach leaves behind any *new* real submodules (e.g.
    openshell._proto.datamodel_pb2, openshell._proto.openshell_pb2_grpc) that
    get imported for the first time while swapped in, since they were never
    part of the original mocked set. That leftover mix of restored mock +
    stray real modules previously corrupted later tests in this file
    (TestExposeServiceEndpoint) that expect a fully-mocked `openshell` --
    `_expose_service()` would resolve `openshell_pb2_grpc` to a fresh
    auto-generated MagicMock attribute rather than the intended stub, and
    urlparse() would then choke on a MagicMock `.url` instead of a string.
    """
    was_mocked = isinstance(sys.modules.get("openshell"), MagicMock)
    if not was_mocked:
        yield
        return
    snapshot = {
        k: v for k, v in sys.modules.items() if k == "openshell" or k.startswith("openshell.")
    }
    for k in list(snapshot):
        del sys.modules[k]
    try:
        yield
    finally:
        for k in [k for k in sys.modules if k == "openshell" or k.startswith("openshell.")]:
            del sys.modules[k]
        sys.modules.update(snapshot)


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

        chunk = '{"type": "tool_call", "name": "a"}\n{"type": "tool_result", "name": "a"}\n'

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
    async def test_spawn_with_credential_does_not_expose_real_value(
        self, mocker: MockerFixture
    ) -> None:
        """Credential via Provider: real value NOT in spec.environment, placeholder via providers (issue #199)."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.exec_stream.return_value = iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        async def mock_ready(*args, **kwargs):
            return True

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")
        mocker.patch.object(OpenShellSpawner, "_build_baseline_filesystem_policy")

        # Mock provider creation to return a fake provider ID
        mock_create_provider = mocker.patch.object(
            spawner, "_create_provider", return_value="provider-123"
        )
        mock_start_server = mocker.patch.object(spawner, "start_server", return_value=None)
        # Ensure credential value is available via os.environ
        mocker.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-real-secret"}, clear=False)

        endpoint = await spawner.spawn(
            "agent-1",
            "sandbox:latest",
            env={"LIGHTSPEED_PROVIDER": "openai", "OPENAI_API_KEY": "sk-real-secret"},
            credential_secret_name="openai-api-key",
        )

        assert endpoint == "http://gateway:17670"
        # Provider should be created with the real credential (env var name, uppercased)
        mock_create_provider.assert_called_once_with(
            credentials={"OPENAI_API_KEY": "sk-real-secret"}
        )
        # Check that spec.environment does NOT contain the real credential
        create_kwargs = mock_client.create.call_args[1]
        spec = create_kwargs["spec"]
        # spec.environment is a proto map, check via dict
        env_dict = dict(spec.environment)
        assert "OPENAI_API_KEY" not in env_dict
        assert "openai-api-key" not in env_dict
        assert "sk-real-secret" not in str(env_dict)
        # But spec.providers should contain the provider ID
        # Check providers: real protobuf has list, mocked spec has append mock
        from unittest.mock import MagicMock

        if isinstance(spec.providers, MagicMock):
            spec.providers.append.assert_called_once_with("provider-123")
        else:
            assert list(spec.providers) == ["provider-123"]
        # Also ensure the spawner stored the provider ID for cleanup
        assert spawner._provider_ids["agent-1"] == "provider-123"
        # Verify start_server env also does not contain real credential (issue #199)
        assert mock_start_server.called
        _, start_kwargs = mock_start_server.call_args
        start_env = start_kwargs.get("env", {})
        assert "OPENAI_API_KEY" not in start_env
        assert "openai-api-key" not in start_env
        assert "sk-real-secret" not in str(start_env)

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

    @pytest.mark.asyncio
    async def test_spawn_starts_server_via_python_module_invocation(
        self, mocker: MockerFixture
    ) -> None:
        """_do_spawn starts uvicorn via `python3 -m uvicorn`, not a bare binary.

        Images that install Python deps with `pip install --target` (a common
        hermetic-build pattern) copy package files but never generate
        console-script executables, so a bare "uvicorn" exec fails with
        "command not found". Invoking it as a module only requires uvicorn to
        be importable, which works regardless of how the image installed it.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        exec_called = threading.Event()
        captured: dict[str, Any] = {}

        def exec_stream_side_effect(_sandbox_id, command, **_kwargs):
            captured["command"] = command
            exec_called.set()
            return iter([])

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.exec_stream.side_effect = exec_stream_side_effect

        spawner = OpenShellSpawner(openshell_client=mock_client)

        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        async def mock_ready(*args, **kwargs):
            return True

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")

        await spawner.spawn("agent-1", "sandbox:latest", env={"K": "V"})

        # start_server() runs exec_stream in a background thread (fire-and-forget);
        # wait on the event it sets instead of a fixed sleep, so the assertion is
        # deterministic rather than racing the background task's scheduling. The
        # wait itself must go through to_thread too — a blocking call here would
        # starve the event loop and prevent the background task from ever running.
        called = await asyncio.to_thread(exec_called.wait, 2.0)
        assert called, "exec_stream was never called"
        assert captured["command"][:3] == ["python3", "-m", "uvicorn"]


class TestOpenShellSpawnerLegacySkillsDeprecation:
    """Tests for the skills_image/skills_paths deprecation warning (issue #202).

    OpenShellSpawner no longer supports the old runtime mount/extract
    mechanism -- skills now ship baked into the sandbox image, scoped
    per-run via allowed_skills instead. A caller still passing the old
    params must be warned, not silently ignored without a trace.
    """

    @staticmethod
    def _make_spawner_and_spawn_kwargs(mocker: MockerFixture):
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.exec_stream = lambda *a, **k: iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        async def mock_ready(*args, **kwargs):
            return True

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")
        return spawner

    @pytest.mark.asyncio
    async def test_skills_image_logs_deprecation_warning(
        self, mocker: MockerFixture, caplog
    ) -> None:
        """Passing skills_image logs a warning naming allowed_skills as the replacement."""
        import logging

        spawner = self._make_spawner_and_spawn_kwargs(mocker)

        with caplog.at_level(logging.WARNING):
            await spawner.spawn(
                "agent-1",
                "sandbox:latest",
                env={},
                skills_image="quay.io/example/skills:latest",
            )

        assert "skills_image" in caplog.text
        assert "allowed_skills" in caplog.text

    @pytest.mark.asyncio
    async def test_no_legacy_skills_params_no_warning(self, mocker: MockerFixture, caplog) -> None:
        """A normal spawn with no legacy params logs nothing about skills_image."""
        import logging

        spawner = self._make_spawner_and_spawn_kwargs(mocker)

        with caplog.at_level(logging.WARNING):
            await spawner.spawn("agent-1", "sandbox:latest", env={})

        assert "skills_image" not in caplog.text


class TestOpenShellSpawnerMaterializeAllowedSkills:
    """Tests for materializing allowed_skills before the server starts (issue #202).

    Landlock's allow-list model can't grant partial directory listing of
    /skills without granting full listing (which would defeat per-name
    scoping): every agent provider discovers skills by *listing*
    LIGHTSPEED_SKILLS_DIR, not by opening a known path directly. So
    OpenShellSpawner execs the sandbox image's baked-in
    materialize-skills.sh (see lightspeed-agentic-sandbox) to physically
    copy just the allowed names into a plain, freshly-listable directory
    before starting the agent server -- the actual Landlock per-name
    grant on /skills/<name> remains the real enforcement; this step is
    what makes provider-side discovery of that scoped subset work at all.
    """

    @staticmethod
    def _make_spawner(mocker: MockerFixture, materialize_exit_code: int = 0):
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        class FakeExecResult:
            def __init__(self, exit_code: int, stderr: str = ""):
                self.exit_code = exit_code
                self.stdout = ""
                self.stderr = stderr

        exec_calls: list[list[str]] = []

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        def exec_stream(sandbox_id, command, **kwargs):
            exec_calls.append(command)
            if command[0].endswith("materialize-skills.sh"):
                return iter([FakeExecResult(materialize_exit_code, stderr="boom")])
            return iter([FakeExecResult(0)])

        mock_client.exec_stream = exec_stream

        spawner = OpenShellSpawner(openshell_client=mock_client)
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        async def mock_ready(*args, **kwargs):
            return True

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")
        return spawner, exec_calls

    @pytest.mark.asyncio
    async def test_execs_materialize_script_with_allowed_skills_as_argv(
        self, mocker: MockerFixture
    ) -> None:
        """allowed_skills names are passed as separate argv elements, not a shell string."""
        spawner, exec_calls = self._make_spawner(mocker)

        await spawner.spawn(
            "agent-1",
            "sandbox:latest",
            env={},
            allowed_skills=["k8s-diag", "git-ops"],
        )

        materialize_calls = [c for c in exec_calls if c[0].endswith("materialize-skills.sh")]
        assert len(materialize_calls) == 1
        assert materialize_calls[0] == [
            "/usr/local/bin/materialize-skills.sh",
            "k8s-diag",
            "git-ops",
        ]

    @pytest.mark.asyncio
    async def test_no_allowed_skills_does_not_exec_materialize_script(
        self, mocker: MockerFixture
    ) -> None:
        """No allowed_skills -- the materialize step is skipped entirely."""
        spawner, exec_calls = self._make_spawner(mocker)

        await spawner.spawn("agent-1", "sandbox:latest", env={})

        materialize_calls = [c for c in exec_calls if c[0].endswith("materialize-skills.sh")]
        assert materialize_calls == []

    @pytest.mark.asyncio
    async def test_materialize_runs_before_server_start(self, mocker: MockerFixture) -> None:
        """The materialize call is awaited (and completes) before start_server is invoked.

        start_server() itself is fire-and-forget (its exec runs in a
        background task), so ordering must be verified at the call-site
        level -- i.e. _materialize_allowed_skills is awaited to completion
        before start_server() is even called -- rather than by racing
        against that background task's own exec_stream call.
        """
        spawner, exec_calls = self._make_spawner(mocker)

        call_order: list[str] = []
        real_materialize = spawner._materialize_allowed_skills

        async def tracked_materialize(sandbox_id, allowed_skills):
            await real_materialize(sandbox_id, allowed_skills)
            call_order.append("materialize")

        async def tracked_start_server(*args, **kwargs):
            call_order.append("start_server")

        mocker.patch.object(spawner, "_materialize_allowed_skills", side_effect=tracked_materialize)
        mocker.patch.object(spawner, "start_server", side_effect=tracked_start_server)

        await spawner.spawn(
            "agent-1",
            "sandbox:latest",
            env={},
            allowed_skills=["k8s-diag"],
        )

        assert call_order == ["materialize", "start_server"]

    @pytest.mark.asyncio
    async def test_rejects_path_traversal_name_before_exec(self, mocker: MockerFixture) -> None:
        """A malicious allowed_skills name is rejected before it ever reaches exec.

        Defense in depth independent of _build_baseline_filesystem_policy()'s
        own validation: advisory-mode spawns (read_only=True) never call
        that method at all, so this exec path must validate independently.
        """
        spawner, exec_calls = self._make_spawner(mocker)

        with pytest.raises(ValueError, match="allowed_skills"):
            await spawner.spawn(
                "agent-1",
                "sandbox:latest",
                env={},
                allowed_skills=["../../etc"],
            )

        materialize_calls = [c for c in exec_calls if c[0].endswith("materialize-skills.sh")]
        assert materialize_calls == []

    @pytest.mark.asyncio
    async def test_materialize_failure_raises_and_blocks_server_start(
        self, mocker: MockerFixture
    ) -> None:
        """A nonzero materialize-skills.sh exit aborts the spawn instead of continuing silently.

        exec_stream() does not raise on a nonzero exit by itself -- without
        an explicit check, a failed materialize (Landlock write denied, or
        an older image predating the script entirely: ENOENT/127) would
        leave the sandbox with no/partial skills while spawn() reported
        success, exactly the failure mode reproduced live against a real
        gateway before this check existed.
        """
        spawner, exec_calls = self._make_spawner(mocker, materialize_exit_code=1)

        with pytest.raises(RuntimeError, match="materialize-skills.sh"):
            await spawner.spawn(
                "agent-1",
                "sandbox:latest",
                env={},
                allowed_skills=["k8s-diag"],
            )

        server_start_calls = [c for c in exec_calls if c[:2] == ["python3", "-m"]]
        assert server_start_calls == []

    @pytest.mark.asyncio
    async def test_advisory_spawn_skips_materialize(self, mocker: MockerFixture) -> None:
        """read_only=True spawns skip materialize entirely, even with allowed_skills set.

        Advisory mode's own Landlock policy (_build_filesystem_policy())
        grants blanket "/" read but no write outside a fixed set of paths
        that doesn't include the materialize destination -- so running
        materialize there would hit the same EACCES this class's other
        tests guard against. Advisory already documents its blanket read
        grant as covering all of /skills regardless of allowed_skills, so
        skipping materialize (and listing the master _SKILLS_ROOT
        directly -- see test_advisory_spawn_lists_skills_root) preserves
        that semantics without needing a second write grant.
        """
        spawner, exec_calls = self._make_spawner(mocker)

        await spawner.spawn(
            "agent-1",
            "sandbox:latest",
            env={},
            allowed_skills=["k8s-diag"],
            read_only=True,
        )

        materialize_calls = [c for c in exec_calls if c[0].endswith("materialize-skills.sh")]
        assert materialize_calls == []

    @pytest.mark.asyncio
    async def test_advisory_spawn_lists_skills_root_not_materialized_dir(
        self, mocker: MockerFixture
    ) -> None:
        """Advisory spawns point LIGHTSPEED_SKILLS_DIR at the master /skills, not /app/skills.

        /app/skills is never materialized in advisory mode (see
        test_advisory_spawn_skips_materialize), so pointing providers
        there would make them discover zero skills -- the opposite of
        advisory's documented "blanket read already covers all of
        /skills" semantics.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner, _ = self._make_spawner(mocker)
        captured_env: dict[str, str] = {}

        async def capture_start_server(sandbox_name, command, env=None):
            captured_env.update(env or {})

        mocker.patch.object(spawner, "start_server", side_effect=capture_start_server)

        await spawner.spawn(
            "agent-1",
            "sandbox:latest",
            env={},
            allowed_skills=["k8s-diag"],
            read_only=True,
        )

        assert captured_env["LIGHTSPEED_SKILLS_DIR"] == OpenShellSpawner._SKILLS_ROOT


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

        mock_client.delete.assert_called_once_with("sb-123", workspace="default")

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

        spawner = OpenShellSpawner(
            openshell_client=mock_client,
        )

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
        mock_client.delete.assert_called_once_with("ca-agent-agent-1", workspace="default")

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

        spawner = OpenShellSpawner(
            openshell_client=mock_client,
        )

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

        mock_client.delete.assert_called_once_with("ca-agent-agent-1", workspace="default")
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

        spawner = OpenShellSpawner(
            openshell_client=mock_client,
        )

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
    async def test_provider_deleted_after_sandbox_cleanup_not_before(
        self, mocker: MockerFixture
    ) -> None:
        """Regression test for issue #214.

        Deleting the Provider before the sandbox is torn down hits
        FAILED_PRECONDITION on a real gateway (the provider is still
        attached to the not-yet-deleted sandbox). _cleanup_sandbox()
        (which detaches the provider from the sandbox) must run first.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        spawner = OpenShellSpawner(openshell_client=mock_client)

        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")
        mocker.patch.object(OpenShellSpawner, "_build_baseline_filesystem_policy")
        mocker.patch.object(spawner, "_create_provider", return_value="provider-123")
        mocker.patch.object(
            spawner,
            "start_server",
            new_callable=mocker.AsyncMock,
            side_effect=RuntimeError("exec failed"),
        )
        mocker.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-real-secret"}, clear=False)

        call_order: list[str] = []

        async def fake_cleanup_sandbox(agent_name: str, sandbox_name: str) -> None:
            call_order.append("cleanup_sandbox")

        async def fake_delete_provider(provider_id: str) -> None:
            call_order.append("delete_provider")

        mocker.patch.object(spawner, "_cleanup_sandbox", side_effect=fake_cleanup_sandbox)
        mocker.patch.object(spawner, "_delete_provider", side_effect=fake_delete_provider)

        with pytest.raises(RuntimeError, match="exec failed"):
            await spawner.spawn(
                "agent-1",
                "sandbox:latest",
                env={"LIGHTSPEED_PROVIDER": "openai", "OPENAI_API_KEY": "sk-real-secret"},
                credential_secret_name="openai-api-key",
            )

        assert call_order == ["cleanup_sandbox", "delete_provider"]

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

        spawner = OpenShellSpawner(
            openshell_client=mock_client,
        )

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


class TestBaselineFilesystemPolicy:
    """Tests for _build_baseline_filesystem_policy() (issue #189).

    Runs for every non-advisory spawn. Unlike _build_filesystem_policy()
    (the advisory/full-lockdown path, read_only=["/"]), this sends
    OpenShell's own default read_only allowlist UNION extra_readable_paths
    -- never just the extras alone, since OpenShell replaces (does not
    merge with) a supplied filesystem policy.
    """

    _DEFAULT_RO = ["/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"]

    def _make_spec(self, mocker: MockerFixture):
        spec = mocker.Mock()
        spec.policy.filesystem.read_only = []
        spec.policy.filesystem.read_write = []
        spec.policy.filesystem.include_workdir = False
        spec.policy.landlock.compatibility = ""
        return spec

    def test_includes_full_default_read_only_allowlist(self, mocker: MockerFixture) -> None:
        """Baseline must include OpenShell's complete default RO list, not just the extras.

        Sending only the extra paths would replace (not merge with)
        OpenShell's default and drop /usr, /lib, /proc, /etc, breaking
        things worse than the bug this fixes.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        for path in self._DEFAULT_RO:
            assert path in spec.policy.filesystem.read_only

    def test_includes_default_extra_readable_paths(self, mocker: MockerFixture) -> None:
        """Baseline includes the default extra paths (/opt/app-root, /opt/lightspeed)."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        assert "/opt/app-root" in spec.policy.filesystem.read_only
        assert "/opt/lightspeed" in spec.policy.filesystem.read_only

    def test_includes_custom_extra_readable_paths(self, mocker: MockerFixture) -> None:
        """A constructor override for extra_readable_paths is honored in the baseline."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object(), extra_readable_paths=["/srv/custom"])
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        assert "/srv/custom" in spec.policy.filesystem.read_only
        # Default RO allowlist is still present alongside the override.
        for path in self._DEFAULT_RO:
            assert path in spec.policy.filesystem.read_only

    def test_sets_default_read_write(self, mocker: MockerFixture) -> None:
        """Baseline read_write matches OpenShell's own default when no skills_image."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        assert spec.policy.filesystem.read_write == ["/tmp", "/dev/null"]

    def test_no_allowed_skills_grants_no_skills_read_access(self, mocker: MockerFixture) -> None:
        """No allowed_skills -- no /skills/* entries at all. Least-privilege default."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        assert not any(p.startswith("/skills") for p in spec.policy.filesystem.read_only)
        assert not any(p.startswith("/skills") for p in spec.policy.filesystem.read_write)

    def test_allowed_skills_grants_read_only_access_per_name(self, mocker: MockerFixture) -> None:
        """Each allowed_skills name grants read-only access to /skills/<name> (issue #202).

        Skills are baked into the sandbox image at build time (see
        lightspeed-agentic-sandbox) -- nothing writes to them at runtime,
        so this is read-only, not read_write like the old skills_image
        tar-upload mechanism needed. OpenShell's read_only access right
        already includes execute (AccessFs::from_read() = Execute |
        ReadFile | ReadDir in the landlock crate OpenShell depends on),
        so skill scripts remain runnable with no separate grant.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec, allowed_skills=["k8s-diag", "git-ops"])

        assert "/skills/k8s-diag" in spec.policy.filesystem.read_only
        assert "/skills/git-ops" in spec.policy.filesystem.read_only
        assert "/skills/k8s-diag" not in spec.policy.filesystem.read_write

    def test_allowed_skills_grants_read_write_on_materialize_destination(
        self, mocker: MockerFixture
    ) -> None:
        """allowed_skills also grants read_write on the materialize destination.

        /app itself is only read_only in the baseline policy, and
        Landlock denies writes regardless of the image's own POSIX
        chmod/chown -- so materialize-skills.sh (which OpenShellSpawner
        execs to copy allowed_skills into this directory, see
        _materialize_allowed_skills()) needs an explicit read_write
        grant here, reproduced as a real EACCES against a live gateway
        before this grant was added.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec, allowed_skills=["k8s-diag"])

        assert spawner._MATERIALIZED_SKILLS_DIR in spec.policy.filesystem.read_write

    def test_no_allowed_skills_does_not_grant_materialize_destination_write(
        self, mocker: MockerFixture
    ) -> None:
        """Omitting allowed_skills grants no write access to the materialize destination."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec, allowed_skills=None)

        assert spawner._MATERIALIZED_SKILLS_DIR not in spec.policy.filesystem.read_write

    def test_allowed_skills_does_not_grant_unlisted_skills(self, mocker: MockerFixture) -> None:
        """Only the named skills get a grant -- others baked into the image stay invisible."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec, allowed_skills=["k8s-diag"])

        assert "/skills/git-ops" not in spec.policy.filesystem.read_only
        assert "/skills/cve-scan" not in spec.policy.filesystem.read_only
        assert "/skills/security-audit" not in spec.policy.filesystem.read_only

    def test_allowed_skills_rejects_path_traversal_name(self, mocker: MockerFixture) -> None:
        """A skill name containing '..' or '/' is rejected, not silently joined.

        allowed_skills entries are meant to be bare directory names
        (e.g. "k8s-diag"), not paths -- a malicious/malformed value like
        "../../etc" must not be allowed to escape /skills via naive
        string joining.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        with pytest.raises(ValueError, match="allowed_skills"):
            spawner._build_baseline_filesystem_policy(spec, allowed_skills=["../../etc"])

    def test_allowed_skills_rejects_empty_name(self, mocker: MockerFixture) -> None:
        """An empty-string skill name is rejected."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        with pytest.raises(ValueError, match="allowed_skills"):
            spawner._build_baseline_filesystem_policy(spec, allowed_skills=[""])

    def test_includes_workdir(self, mocker: MockerFixture) -> None:
        """Baseline sets include_workdir."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        assert spec.policy.filesystem.include_workdir is True

    def test_sets_landlock_best_effort_compatibility(self, mocker: MockerFixture) -> None:
        """Landlock compatibility is explicitly set to best_effort.

        Matches the live-verified fix YAML in issue #189, even though
        best_effort is already the proto default -- an explicit setting
        survives even if the proto default ever changes.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        assert spec.policy.landlock.compatibility == "best_effort"

    def test_empty_extra_readable_paths_still_sets_defaults(self, mocker: MockerFixture) -> None:
        """An explicitly empty extra_readable_paths list doesn't drop the OpenShell defaults."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object(), extra_readable_paths=[])
        spec = self._make_spec(mocker)

        spawner._build_baseline_filesystem_policy(spec)

        for path in self._DEFAULT_RO:
            assert path in spec.policy.filesystem.read_only
        assert "/opt/app-root" not in spec.policy.filesystem.read_only


class TestFilesystemPolicySelection:
    """Tests that _do_spawn picks exactly one filesystem policy builder (issue #189).

    Baseline and advisory are mutually exclusive: baseline runs for every
    non-advisory spawn (read_only=False, the default for normal agent
    runs), and the existing advisory full-lockdown builder is unchanged
    and still only runs when read_only=True.
    """

    def _make_spawner_ready_to_spawn(self, mocker: MockerFixture):
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.exec_stream.return_value = iter([])

        spawner = OpenShellSpawner(openshell_client=mock_client)
        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        async def mock_ready(*args, **kwargs):
            return True

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")
        return spawner

    @pytest.mark.asyncio
    async def test_non_advisory_spawn_uses_baseline_not_advisory(
        self, mocker: MockerFixture
    ) -> None:
        """A normal (non-advisory) spawn builds the baseline policy, not the full lockdown."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = self._make_spawner_ready_to_spawn(mocker)
        baseline_spy = mocker.patch.object(
            OpenShellSpawner, "_build_baseline_filesystem_policy", autospec=True
        )
        advisory_spy = mocker.patch.object(OpenShellSpawner, "_build_filesystem_policy")

        await spawner.spawn("agent-1", "sandbox:latest", env={}, read_only=False)

        baseline_spy.assert_called_once()
        advisory_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_advisory_spawn_uses_advisory_not_baseline(self, mocker: MockerFixture) -> None:
        """An advisory (read_only=True) spawn keeps using the existing full-lockdown builder."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = self._make_spawner_ready_to_spawn(mocker)
        baseline_spy = mocker.patch.object(
            OpenShellSpawner, "_build_baseline_filesystem_policy", autospec=True
        )
        advisory_spy = mocker.patch.object(OpenShellSpawner, "_build_filesystem_policy")

        await spawner.spawn("agent-1", "sandbox:latest", env={}, read_only=True)

        advisory_spy.assert_called_once()
        baseline_spy.assert_not_called()


class TestExtraReadablePathsConstructor:
    """Tests for the `extra_readable_paths` constructor argument (issue #189).

    These paths let a baseline (non-advisory) filesystem policy include
    directories beyond OpenShell's own hardcoded default allowlist --
    e.g. /opt/app-root and /opt/lightspeed, where this sandbox image's
    Python packages and application code live.
    """

    def test_defaults_to_opt_app_root_and_opt_lightspeed(self) -> None:
        """Constructor default matches the reference sandbox image's layout."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())

        assert spawner._extra_readable_paths == ["/opt/app-root", "/opt/lightspeed"]

    def test_accepts_custom_paths(self) -> None:
        """Constructor accepts an override for derived images with a different layout."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(
            openshell_client=object(),
            extra_readable_paths=["/opt/custom", "/srv/app"],
        )

        assert spawner._extra_readable_paths == ["/opt/custom", "/srv/app"]

    def test_rejects_relative_path(self) -> None:
        """A relative path is not a valid Landlock filesystem rule."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match="absolute"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["relative/path"])

    def test_rejects_dotdot_segment(self) -> None:
        """A path containing a `..` segment could escape the intended directory."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match=r"\.\."):
            OpenShellSpawner(
                openshell_client=object(), extra_readable_paths=["/opt/app-root/../etc"]
            )

    def test_rejects_empty_string(self) -> None:
        """An empty string is not a meaningful filesystem path."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match="empty"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["/opt/app-root", ""])

    def test_empty_list_is_valid(self) -> None:
        """An explicit empty list disables the extra-paths feature entirely."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object(), extra_readable_paths=[])

        assert spawner._extra_readable_paths == []

    def test_rejects_filesystem_root(self) -> None:
        """ "/" widens the baseline policy to full-filesystem read (issue #189 review).

        The baseline policy's write list stays narrow (/tmp, /dev/null),
        but "/" in read_only would grant read access to everything --
        effectively disabling the read restriction this fix exists to
        preserve, without any of advisory mode's compensating write
        lockdown.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match="root"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["/"])

    def test_rejects_filesystem_root_via_extra_slashes(self) -> None:
        """ "//" and "///" are root-equivalent, caught by the strip("/") check."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match="root"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["//"])
        with pytest.raises(ValueError, match="root"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["///"])

    def test_rejects_filesystem_root_via_dot_segments(self) -> None:
        """ "/." and "/././" are root-equivalent once path-resolved, caught by normpath.

        strip("/") alone is not enough here: "/.".strip("/") == "." (not
        empty), since strip() only trims leading/trailing "/" characters
        and doesn't resolve "." segments. Landlock/kernel path resolution
        treats "/." the same as "/", so this needs the normpath() check
        combined with the strip() check, not either alone.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match="root"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["/."])
        with pytest.raises(ValueError, match="root"):
            OpenShellSpawner(openshell_client=object(), extra_readable_paths=["/././"])


class TestExtraEnvConstructor:
    """Tests for the `extra_env` constructor argument (issue #192).

    OpenShell's supervisor calls env_clear() before exec'ing the server
    process (ssh.rs apply_child_env()), then rebuilds the environment from
    a hardcoded allowlist plus whatever the caller passes as env= to
    exec()/exec_stream() (OPENSHELL_USER_ENVIRONMENT). The reference
    sandbox image needs PYTHONPATH to import its own lightspeed_agentic
    module (installed at /opt/lightspeed/src, outside the interpreter's
    default site-packages) -- but nothing ever set it, so every
    non-advisory spawn failed with "HTTP server did not become ready".
    """

    def test_defaults_to_reference_image_pythonpath(self) -> None:
        """Constructor default matches the reference sandbox image's PYTHONPATH."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())

        assert spawner._extra_env == {
            "PYTHONPATH": "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages",
            "LIGHTSPEED_SKILLS_DIR": "/app/skills",
        }

    def test_defaults_include_lightspeed_skills_dir(self) -> None:
        """Constructor default sets LIGHTSPEED_SKILLS_DIR to the materialized-skills path.

        Same env_clear() problem as PYTHONPATH (issue #192): the sandbox
        image's own `ENV LIGHTSPEED_SKILLS_DIR=/app/skills` declaration
        is wiped before the exec'd server process starts, and providers
        need to list /app/skills specifically (the materialize-skills.sh
        output), not the read-only /skills master (issue #202).
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object())

        assert spawner._extra_env["LIGHTSPEED_SKILLS_DIR"] == "/app/skills"

    def test_accepts_custom_env(self) -> None:
        """Constructor accepts an override for derived images with a different layout."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(
            openshell_client=object(),
            extra_env={"PYTHONPATH": "/custom/path"},
        )

        assert spawner._extra_env == {"PYTHONPATH": "/custom/path"}

    def test_empty_dict_disables_the_feature(self) -> None:
        """An explicit empty dict means no extra env is injected."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object(), extra_env={})

        assert spawner._extra_env == {}

    def test_none_falls_back_to_default(self) -> None:
        """Explicit None (e.g. an unset Pydantic Optional field) behaves like omitted."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=object(), extra_env=None)

        assert spawner._extra_env == {
            "PYTHONPATH": "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages",
            "LIGHTSPEED_SKILLS_DIR": "/app/skills",
        }

    def test_rejects_empty_key(self) -> None:
        """An empty env var name is not meaningful."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        with pytest.raises(ValueError, match="empty"):
            OpenShellSpawner(openshell_client=object(), extra_env={"": "value"})

    def test_caller_does_not_mutate_stored_default(self) -> None:
        """Mutating the dict passed in must not retroactively change the spawner's policy."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        source = {"PYTHONPATH": "/custom"}
        spawner = OpenShellSpawner(openshell_client=object(), extra_env=source)
        source["PYTHONPATH"] = "/mutated"

        assert spawner._extra_env == {"PYTHONPATH": "/custom"}


class TestExtraEnvMergedIntoServerExec:
    """Tests that extra_env actually reaches start_server()'s exec call (issue #192).

    Unit tests can only assert this merge happens correctly -- they can't
    catch the underlying bug itself, since that's about what OpenShell's
    real supervisor does with the exec environment, which no mock
    reproduces. See tests/e2e/test_guardrails.py for the real-gateway
    regression test.
    """

    def _make_spawner_for_do_spawn(self, mocker: MockerFixture, extra_env=None):
        """Build a spawner with _do_spawn's collaborators mocked except start_server."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        class SandboxRef:
            id: str = "test-id"

            def __init__(self, name):
                self.name = name

        mock_client = mocker.Mock()
        mock_client.create.return_value = SandboxRef("ca-agent-agent-1")
        mock_client.wait_ready.return_value = SandboxRef("ca-agent-agent-1")

        kwargs = {} if extra_env is None else {"extra_env": extra_env}
        spawner = OpenShellSpawner(openshell_client=mock_client, **kwargs)

        mocker.patch.object(
            spawner,
            "_expose_service",
            return_value=("http://gateway:17670", "sandbox.openshell.localhost"),
        )

        async def mock_ready(*args, **kwargs):
            return True

        mocker.patch.object(spawner, "_wait_ready_with_host", side_effect=mock_ready)
        mocker.patch.object(OpenShellSpawner, "_build_network_policy")
        mocker.patch.object(spawner, "start_server", new=mocker.AsyncMock())

        return spawner

    @pytest.mark.asyncio
    async def test_default_extra_env_merged_into_start_server_call(
        self, mocker: MockerFixture
    ) -> None:
        """The default PYTHONPATH reaches start_server()'s env, alongside caller env."""
        spawner = self._make_spawner_for_do_spawn(mocker)

        await spawner.spawn("agent-1", "sandbox:latest", env={"LIGHTSPEED_PROVIDER": "openai"})

        call_kwargs = spawner.start_server.call_args.kwargs
        merged_env = call_kwargs["env"]
        assert merged_env["LIGHTSPEED_PROVIDER"] == "openai"
        assert merged_env["PYTHONPATH"] == (
            "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages"
        )

    @pytest.mark.asyncio
    async def test_caller_env_wins_on_key_collision(self, mocker: MockerFixture) -> None:
        """An explicit caller-provided PYTHONPATH overrides the spawner's default.

        Lets a caller targeting a different derived image override the
        constructor default per-spawn, without needing a second spawner
        instance.
        """
        spawner = self._make_spawner_for_do_spawn(mocker)

        await spawner.spawn(
            "agent-1",
            "sandbox:latest",
            env={"LIGHTSPEED_PROVIDER": "openai", "PYTHONPATH": "/caller/override"},
        )

        call_kwargs = spawner.start_server.call_args.kwargs
        assert call_kwargs["env"]["PYTHONPATH"] == "/caller/override"

    @pytest.mark.asyncio
    async def test_empty_extra_env_leaves_caller_env_unchanged(self, mocker: MockerFixture) -> None:
        """extra_env=={} means only the caller's own env reaches start_server()."""
        spawner = self._make_spawner_for_do_spawn(mocker, extra_env={})

        await spawner.spawn("agent-1", "sandbox:latest", env={"LIGHTSPEED_PROVIDER": "openai"})

        call_kwargs = spawner.start_server.call_args.kwargs
        assert call_kwargs["env"] == {"LIGHTSPEED_PROVIDER": "openai"}


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
            spawner,
            "_create_and_attach_provider",
            return_value="provider-123",
        )

        await spawner._inject_credentials(
            "agent-1",
            "sb-1",
            "OPENAI_API_KEY",
            {"OPENAI_API_KEY": "sk-test"},
        )

        mock_create.assert_called_once_with(
            "sb-1",
            credentials={"OPENAI_API_KEY": "sk-test"},
        )
        assert spawner._provider_ids["agent-1"] == "provider-123"

    @pytest.mark.asyncio
    async def test_provider_failure_does_not_fallback_to_file(self, mocker: MockerFixture) -> None:
        """Provider failure no longer falls back to file injection (issue #199)."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)
        spawner._sandbox_ids["agent-1"] = "uuid-1"

        mocker.patch.object(
            spawner,
            "_create_and_attach_provider",
            side_effect=Exception("gRPC unavailable"),
        )
        mock_file_inject = mocker.patch.object(
            spawner,
            "_inject_credentials_via_files",
        )

        with pytest.raises(Exception, match="gRPC unavailable"):
            await spawner._inject_credentials(
                "agent-1",
                "sb-1",
                "OPENAI_API_KEY",
                {"OPENAI_API_KEY": "sk-test"},
            )

        mock_file_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_credential_not_in_env(self, mocker: MockerFixture) -> None:
        """Raises RuntimeError when credential key not found in env."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        with pytest.raises(RuntimeError, match="not found in env"):
            await spawner._inject_credentials("agent-1", "sb-1", "MISSING_KEY", {})

    def test_provider_uses_datamodel_module(self):
        """Regression for issue #211: Provider lives in datamodel_pb2, not openshell_pb2."""
        from pathlib import Path

        # Check the source file directly -- avoids MagicMock stub in this test module
        # (which replaces openshell with a mock when the extra is not installed)
        src = Path("src/cloud_agents/spawner/openshell_spawner.py").read_text()
        # Both provider creation sites must use datamodel_pb2.Provider
        assert src.count("datamodel_pb2.Provider") == 2, (
            "Expected 2 uses of datamodel_pb2.Provider (for _create_provider and "
            "_create_and_attach_provider), found %d" % src.count("datamodel_pb2.Provider")
        )
        assert (
            "openshell_pb2.Provider" not in src
        ), "Found stale openshell_pb2.Provider -- should be datamodel_pb2.Provider (issue #211)"
        # Also verify the import is present
        assert "from openshell._proto import datamodel_pb2" in src

        # If the real openshell package is available, also verify the proto descriptor
        try:
            with _real_openshell_modules():
                from openshell._proto import datamodel_pb2, openshell_pb2

                req = openshell_pb2.CreateProviderRequest(
                    provider=datamodel_pb2.Provider(
                        type="cloud-agents",
                        credentials={"OPENAI_API_KEY": "test"},
                    ),
                )
                assert req.provider.type == "cloud-agents"
                assert (
                    openshell_pb2.CreateProviderRequest.DESCRIPTOR.fields_by_name[
                        "provider"
                    ].message_type.full_name
                    == "openshell.datamodel.v1.Provider"
                )
                assert not hasattr(openshell_pb2, "Provider")
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")


class TestProviderResponseMessages:
    """Tests for correct parsing of real OpenShell provider response messages.

    These construct real protobuf objects from openshell._proto (not plain
    Mocks, which auto-create any attribute access and would hide exactly
    this class of bug) to verify the spawner code reads the right
    fields/attributes -- confirmed against a real gateway that several of
    these were wrong (see issue #211 and its follow-ups).
    """

    def test_provider_metadata_has_no_top_level_id(self) -> None:
        """Provider's identifying fields live under metadata, not top-level.

        Structural check only -- see test_create_provider_returns_metadata_name
        below for the actual behavioral assertion (which field _create_provider()
        must read). Confirmed against a real gateway that reading metadata.id
        here (instead of metadata.name) makes CreateSandbox fail with
        "provider '<id>' not found" -- spec.providers/AttachSandboxProvider/
        DetachSandboxProvider all resolve providers by name.
        """
        try:
            with _real_openshell_modules():
                from openshell._proto import datamodel_pb2

                provider = datamodel_pb2.Provider(
                    type="cloud-agents",
                    metadata=datamodel_pb2.ObjectMeta(
                        id="provider-uuid-12345",
                        name="test-provider",
                    ),
                )
                assert provider.metadata.id == "provider-uuid-12345"
                assert not hasattr(provider, "id"), "Provider should not have top-level id field"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")

    @pytest.mark.asyncio
    async def test_create_provider_returns_metadata_name(self, mocker: MockerFixture) -> None:
        """_create_provider() must return metadata.name, not metadata.id.

        id and name are set to different values here specifically so a wrong
        extraction is caught rather than coincidentally passing.
        """
        try:
            with _real_openshell_modules():
                from openshell._proto import datamodel_pb2, openshell_pb2

                from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

                real_response = openshell_pb2.ProviderResponse(
                    provider=datamodel_pb2.Provider(
                        type="cloud-agents",
                        metadata=datamodel_pb2.ObjectMeta(
                            id="provider-uuid-should-not-be-returned",
                            name="provider-name-should-be-returned",
                        ),
                    )
                )

                mock_stub_cls = mocker.patch(
                    "openshell._proto.openshell_pb2_grpc.OpenShellStub",
                )
                mock_stub_cls.return_value.CreateProvider.return_value = real_response

                spawner = OpenShellSpawner(
                    openshell_client=mocker.Mock(),
                    tls_ca="/tmp/fake-ca.pem",
                )
                mocker.patch.object(spawner, "_create_grpc_channel")

                result = await spawner._create_provider(credentials={"OPENAI_API_KEY": "sk-test"})

                assert result == "provider-name-should-be-returned"
                assert result != "provider-uuid-should-not-be-returned"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")

    @pytest.mark.asyncio
    async def test_create_provider_returns_metadata_name_ci(self, mocker: MockerFixture) -> None:
        """CI-runnable version of test_create_provider_returns_metadata_name.

        That test skips in CI (the openshell extra isn't installed there --
        the same gap that let #211/#213's bugs ship). This one doesn't need
        the real package: it controls CreateProvider's return value directly
        via mocker.patch, using types.SimpleNamespace (not MagicMock) so a
        wrong attribute access (.provider.id instead of .provider.metadata.name)
        raises AttributeError instead of silently returning another mock,
        regardless of whether openshell is actually installed.
        """
        from types import SimpleNamespace

        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        fake_response = SimpleNamespace(
            provider=SimpleNamespace(
                metadata=SimpleNamespace(name="provider-name-should-be-returned"),
            ),
        )

        mock_stub_cls = mocker.patch(
            "openshell._proto.openshell_pb2_grpc.OpenShellStub",
        )
        mock_stub_cls.return_value.CreateProvider.return_value = fake_response
        mocker.patch("openshell._proto.openshell_pb2.CreateProviderRequest")
        mocker.patch("openshell._proto.datamodel_pb2.Provider")

        spawner = OpenShellSpawner(
            openshell_client=mocker.Mock(),
            tls_ca="/tmp/fake-ca.pem",
        )
        mocker.patch.object(spawner, "_create_grpc_channel")

        result = await spawner._create_provider(credentials={"OPENAI_API_KEY": "sk-test"})

        assert result == "provider-name-should-be-returned"

    def test_provider_request_has_workspace_field(self) -> None:
        """Regression: CreateProviderRequest must include workspace parameter.

        Verify that CreateProviderRequest accepts and stores a workspace field,
        which is required for the provider to be created in the correct workspace.
        """
        try:
            with _real_openshell_modules():
                from openshell._proto import datamodel_pb2, openshell_pb2

                req = openshell_pb2.CreateProviderRequest(
                    workspace="test-workspace",
                    provider=datamodel_pb2.Provider(
                        type="cloud-agents",
                        credentials={"KEY": "value"},
                    ),
                )
                assert req.workspace == "test-workspace"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")

    def test_attach_sandbox_provider_request_fields(self) -> None:
        """Regression: AttachSandboxProviderRequest uses sandbox_name and provider_name fields.

        Verify the correct field names for attaching a provider to a sandbox.
        """
        try:
            with _real_openshell_modules():
                from openshell._proto import openshell_pb2

                req = openshell_pb2.AttachSandboxProviderRequest(
                    sandbox_name="test-sandbox",
                    provider_name="test-provider-id",
                    workspace="test-workspace",
                )
                assert req.sandbox_name == "test-sandbox"
                assert req.provider_name == "test-provider-id"
                assert req.workspace == "test-workspace"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")

    def test_detach_sandbox_provider_request_fields(self) -> None:
        """Regression: DetachSandboxProviderRequest uses sandbox_name and provider_name fields."""
        try:
            with _real_openshell_modules():
                from openshell._proto import openshell_pb2

                req = openshell_pb2.DetachSandboxProviderRequest(
                    sandbox_name="test-sandbox",
                    provider_name="test-provider-id",
                    workspace="test-workspace",
                )
                assert req.sandbox_name == "test-sandbox"
                assert req.provider_name == "test-provider-id"
                assert req.workspace == "test-workspace"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")

    def test_delete_provider_request_fields(self) -> None:
        """Regression: DeleteProviderRequest uses 'name' field for provider ID."""
        try:
            with _real_openshell_modules():
                from openshell._proto import openshell_pb2

                req = openshell_pb2.DeleteProviderRequest(
                    name="test-provider-id",
                    workspace="test-workspace",
                )
                assert req.name == "test-provider-id"
                assert req.workspace == "test-workspace"
        except (ImportError, ModuleNotFoundError):
            pytest.skip("openshell not installed")


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
            "agent-1",
            mounts,
            {"my-secret": "secret-value"},
        )

        mock_mkdir.assert_called_once_with("uuid-1", "/var/secrets/mcp/kubectl/")
        mock_write.assert_called_once_with(
            "agent-1",
            "/var/secrets/mcp/kubectl/api-key",
            "secret-value",
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
            "agent-1",
            "image:latest",
            env={},
            tls_certs=tls,
        )

        info_calls = [
            str(c)
            for c in mock_logger.info.call_args_list
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
            "agent-1",
            "image:latest",
            env={},
            service_account="my-sa",
        )

        info_calls = [
            str(c)
            for c in mock_logger.info.call_args_list
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
            spawner,
            "_detach_provider",
            side_effect=Exception("detach failed"),
        )
        mock_client.delete = mocker.Mock()

        await spawner._do_destroy("agent-1")

        mock_client.delete.assert_called_once_with("sb-1", workspace="default")


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

    def test_provider_url_suppresses_default_provider_host(self, mocker: MockerFixture) -> None:
        """LIGHTSPEED_PROVIDER_URL set alongside a known LIGHTSPEED_PROVIDER
        routes exclusively through the custom URL -- no direct egress to the
        vendor's public host (issue #209 gap 2)."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {
            "LIGHTSPEED_PROVIDER": "openai",
            "LIGHTSPEED_PROVIDER_URL": "https://inference.local/v1",
        }
        OpenShellSpawner._build_network_policy(spec, env)

        assert "llm_provider" not in spec.policy.network_policies
        assert "custom_provider" in spec.policy.network_policies
        np = spec.policy.network_policies["custom_provider"]
        assert np._ep.host == "inference.local"

    def test_provider_url_unparseable_warns_and_adds_no_egress(
        self, mocker: MockerFixture, caplog: Any
    ) -> None:
        """An unparseable LIGHTSPEED_PROVIDER_URL fails closed: no default
        provider egress (suppressed because the URL is set) and no
        custom_provider egress (no hostname to route to) -- but a warning
        is logged so the misconfiguration isn't silent."""
        import logging

        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spec = self._make_mock_spec(mocker)
        env = {
            "LIGHTSPEED_PROVIDER": "openai",
            "LIGHTSPEED_PROVIDER_URL": "   ",
        }
        with caplog.at_level(logging.WARNING):
            OpenShellSpawner._build_network_policy(spec, env)

        assert "llm_provider" not in spec.policy.network_policies
        assert "custom_provider" not in spec.policy.network_policies
        assert "no parseable hostname" in caplog.text

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


class TestResolveGrpcTarget:
    """Tests for _resolve_grpc_target() endpoint resolution."""

    def test_uses_explicit_endpoint(self, mocker: MockerFixture) -> None:
        """Explicit endpoint parameter takes precedence over client."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        mock_client._endpoint = "client-host:17670"
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="explicit-host:17670",
        )

        assert spawner._resolve_grpc_target() == "explicit-host:17670"

    def test_falls_back_to_client_endpoint(self, mocker: MockerFixture) -> None:
        """Falls back to client._endpoint when endpoint is empty."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        mock_client._endpoint = "client-host:17670"
        spawner = OpenShellSpawner(openshell_client=mock_client)

        assert spawner._resolve_grpc_target() == "client-host:17670"

    def test_strips_http_scheme(self, mocker: MockerFixture) -> None:
        """Strips http:// prefix from endpoint."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="http://host:17670",
        )

        assert spawner._resolve_grpc_target() == "host:17670"

    def test_strips_https_scheme(self, mocker: MockerFixture) -> None:
        """Strips https:// prefix from endpoint."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="https://host:17670",
        )

        assert spawner._resolve_grpc_target() == "host:17670"


class TestCreateGrpcChannel:
    """Tests for _create_grpc_channel() TLS configuration."""

    def _mock_grpc(self, mocker: MockerFixture) -> MagicMock:
        """Set up grpc module mock for channel tests."""
        mock_grpc = MagicMock()
        mocker.patch.dict("sys.modules", {"grpc": mock_grpc})
        return mock_grpc

    def test_insecure_channel_without_tls(self, mocker: MockerFixture) -> None:
        """Creates insecure channel when no TLS is configured."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_grpc = self._mock_grpc(mocker)

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="host:17670",
        )

        spawner._create_grpc_channel()

        mock_grpc.insecure_channel.assert_called_once_with("host:17670")

    def test_secure_channel_with_tls_ca(self, mocker: MockerFixture, tmp_path) -> None:
        """Creates secure channel with CA cert."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_grpc = self._mock_grpc(mocker)

        ca_file = tmp_path / "ca.pem"
        ca_file.write_bytes(b"fake-ca-cert")

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="host:17670",
            tls_ca=str(ca_file),
        )

        mock_grpc.ssl_channel_credentials.return_value = "creds"

        spawner._create_grpc_channel()

        mock_grpc.secure_channel.assert_called_once_with("host:17670", "creds")

    def test_secure_channel_with_mtls(self, mocker: MockerFixture, tmp_path) -> None:
        """Creates secure channel with client cert and key for mTLS."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_grpc = self._mock_grpc(mocker)

        ca_file = tmp_path / "ca.pem"
        ca_file.write_bytes(b"fake-ca")
        cert_file = tmp_path / "client.pem"
        cert_file.write_bytes(b"fake-cert")
        key_file = tmp_path / "client.key"
        key_file.write_bytes(b"fake-key")

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="host:17670",
            tls_ca=str(ca_file),
            tls_cert=str(cert_file),
            tls_key=str(key_file),
        )

        mock_grpc.ssl_channel_credentials.return_value = "creds"

        spawner._create_grpc_channel()

        mock_grpc.ssl_channel_credentials.assert_called_once_with(
            root_certificates=b"fake-ca",
            private_key=b"fake-key",
            certificate_chain=b"fake-cert",
        )

    def test_bearer_token_uses_composite_credentials(self, mocker: MockerFixture, tmp_path) -> None:
        """Bearer token + TLS uses composite_channel_credentials."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_grpc = self._mock_grpc(mocker)

        ca_file = tmp_path / "ca.pem"
        ca_file.write_bytes(b"fake-ca")

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="host:17670",
            tls_ca=str(ca_file),
            bearer_token="my-token",
        )

        mock_grpc.ssl_channel_credentials.return_value = "ssl-creds"
        mock_grpc.access_token_call_credentials.return_value = "call-creds"
        mock_grpc.composite_channel_credentials.return_value = "composite-creds"

        spawner._create_grpc_channel()

        mock_grpc.access_token_call_credentials.assert_called_once_with("my-token")
        mock_grpc.composite_channel_credentials.assert_called_once_with("ssl-creds", "call-creds")
        mock_grpc.secure_channel.assert_called_once_with("host:17670", "composite-creds")

    def test_bearer_token_without_tls_raises(self, mocker: MockerFixture) -> None:
        """Bearer token without TLS raises ValueError — fail closed."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        self._mock_grpc(mocker)

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="host:17670",
            bearer_token="my-token",
        )

        with pytest.raises(ValueError, match="requires TLS"):
            spawner._create_grpc_channel()


class TestExposeServiceEndpoint:
    """Tests for _expose_service() endpoint routing (#175)."""

    @pytest.mark.asyncio
    async def test_defaults_to_grpc_endpoint(self, mocker: MockerFixture) -> None:
        """Without override, uses gRPC endpoint as HTTP base URL."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="gw:17670",
        )

        mock_resp = mocker.Mock()
        mock_resp.url = "http://sandbox.openshell.localhost:8080"

        mock_stub_cls = mocker.patch(
            "openshell._proto.openshell_pb2_grpc.OpenShellStub",
        )
        mock_stub = mock_stub_cls.return_value
        mock_stub.ExposeService.return_value = mock_resp

        mocker.patch.object(spawner, "_create_grpc_channel")

        endpoint_url, virtual_host = await spawner._expose_service("sb-1", 8080)

        assert endpoint_url == "http://gw:17670"
        assert virtual_host == "sandbox.openshell.localhost"

    @pytest.mark.asyncio
    async def test_defaults_to_https_with_tls(self, mocker: MockerFixture) -> None:
        """With TLS configured, default endpoint uses https scheme."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="gw:17670",
            tls_ca="/tmp/ca.pem",
        )

        mock_resp = mocker.Mock()
        mock_resp.url = "https://sandbox.openshell.localhost:8080"

        mock_stub_cls = mocker.patch(
            "openshell._proto.openshell_pb2_grpc.OpenShellStub",
        )
        mock_stub = mock_stub_cls.return_value
        mock_stub.ExposeService.return_value = mock_resp

        mocker.patch.object(spawner, "_create_grpc_channel")

        endpoint_url, virtual_host = await spawner._expose_service("sb-1", 8080)

        assert endpoint_url == "https://gw:17670"

    @pytest.mark.asyncio
    async def test_http_endpoint_override(self, mocker: MockerFixture) -> None:
        """http_endpoint overrides the gateway-returned URL."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            endpoint="gw:17670",
            http_endpoint="https://external-proxy.example.com",
        )

        mock_resp = mocker.Mock()
        mock_resp.url = "http://internal-gw:17670"

        mock_stub_cls = mocker.patch(
            "openshell._proto.openshell_pb2_grpc.OpenShellStub",
        )
        mock_stub = mock_stub_cls.return_value
        mock_stub.ExposeService.return_value = mock_resp

        mocker.patch.object(spawner, "_create_grpc_channel")

        endpoint_url, virtual_host = await spawner._expose_service("sb-1", 8080)

        assert endpoint_url == "https://external-proxy.example.com"
        assert virtual_host == "internal-gw"


class TestGetQuerySslContext:
    """Tests for get_query_ssl_context() (#194).

    step_runner.py's query-time HTTP client had no way to learn about
    this spawner's own TLS config, so it fell back to httpx's default
    system trust store -- which doesn't include a self-signed OpenShell
    gateway CA. This method exposes exactly the same SSL context
    construction _wait_ready_with_host() already builds internally, so
    both call sites share one implementation.
    """

    def test_returns_none_without_tls_ca(self, mocker: MockerFixture) -> None:
        """No tls_ca configured -> None (caller falls back to its own default)."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client)

        assert spawner.get_query_ssl_context() is None

    def test_returns_ssl_context_with_tls_ca(self, mocker: MockerFixture) -> None:
        """tls_ca configured -> SSLContext built from that CA file."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_ssl_ctx = mocker.Mock()
        mock_create_default_context = mocker.patch(
            "cloud_agents.spawner.openshell_spawner.ssl.create_default_context",
            return_value=mock_ssl_ctx,
        )

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client, tls_ca="/etc/openshell-tls/ca.crt")

        result = spawner.get_query_ssl_context()

        mock_create_default_context.assert_called_once_with(cafile="/etc/openshell-tls/ca.crt")
        assert result is mock_ssl_ctx
        mock_ssl_ctx.load_cert_chain.assert_not_called()

    def test_returns_ssl_context_with_mtls_client_cert(self, mocker: MockerFixture) -> None:
        """tls_cert/tls_key configured -> client cert chain loaded onto the context."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_ssl_ctx = mocker.Mock()
        mocker.patch(
            "cloud_agents.spawner.openshell_spawner.ssl.create_default_context",
            return_value=mock_ssl_ctx,
        )

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(
            openshell_client=mock_client,
            tls_ca="/etc/openshell-tls/ca.crt",
            tls_cert="/etc/openshell-tls/client.crt",
            tls_key="/etc/openshell-tls/client.key",
        )

        result = spawner.get_query_ssl_context()

        mock_ssl_ctx.load_cert_chain.assert_called_once_with(
            "/etc/openshell-tls/client.crt", "/etc/openshell-tls/client.key"
        )
        assert result is mock_ssl_ctx

    @pytest.mark.asyncio
    async def test_wait_ready_with_host_uses_same_context(self, mocker: MockerFixture) -> None:
        """_wait_ready_with_host()'s own SSL context matches get_query_ssl_context()'s.

        Regression guard: the two must share one implementation, not two
        copies that can silently drift apart.
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_ssl_ctx = mocker.Mock()
        mocker.patch(
            "cloud_agents.spawner.openshell_spawner.ssl.create_default_context",
            return_value=mock_ssl_ctx,
        )

        mock_client = mocker.Mock()
        spawner = OpenShellSpawner(openshell_client=mock_client, tls_ca="/etc/openshell-tls/ca.crt")

        mock_response = mocker.Mock(status_code=200)
        mock_http_client = mocker.AsyncMock()
        mock_http_client.get = mocker.AsyncMock(return_value=mock_response)
        mock_async_client_cls = mocker.patch(
            "cloud_agents.spawner.openshell_spawner.httpx.AsyncClient"
        )
        mock_async_client_cls.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_http_client
        )
        mock_async_client_cls.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await spawner._wait_ready_with_host("http://gw:8080", "vh", timeout=1.0)

        assert mock_async_client_cls.call_args.kwargs["verify"] is mock_ssl_ctx


class TestWaitReadyIngressMismatchDiagnostic:
    """Tests for _wait_ready_with_host()'s issue #209 fail-fast diagnostic.

    A repeated bare 404 (no body, no content-type) is the fingerprint of
    an ingress that doesn't support Host-header-based HTTP routing to
    sandbox ports on its main TLS port -- not a slow-starting sandbox.
    Raising early with a clear message beats silently waiting out the
    full timeout for a generic "did not become ready" error.
    """

    def _mock_http_client(self, mocker: MockerFixture, responses: list) -> Any:
        """Patch httpx.AsyncClient so client.get() yields `responses` in order."""
        mocker.patch(
            "cloud_agents.spawner.openshell_spawner.asyncio.sleep",
            new=mocker.AsyncMock(),
        )
        mock_http_client = mocker.AsyncMock()
        mock_http_client.get = mocker.AsyncMock(side_effect=responses)
        mock_async_client_cls = mocker.patch(
            "cloud_agents.spawner.openshell_spawner.httpx.AsyncClient"
        )
        mock_async_client_cls.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_http_client
        )
        mock_async_client_cls.return_value.__aexit__ = mocker.AsyncMock(return_value=False)
        return mock_http_client

    @pytest.mark.asyncio
    async def test_raises_on_repeated_bare_404(self, mocker: MockerFixture) -> None:
        """5 consecutive bare 404s raise a diagnostic error, not a plain timeout."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.Mock())
        bare_404 = mocker.Mock(status_code=404, content=b"", headers={})
        http_client = self._mock_http_client(mocker, [bare_404] * 5)

        with pytest.raises(RuntimeError) as exc_info:
            await spawner._wait_ready_with_host(
                "https://gw:443", "default--sb.openshell.localhost", timeout=60.0
            )

        assert "bare 404" in str(exc_info.value)
        assert "issue #209" in str(exc_info.value)
        assert "default--sb.openshell.localhost" in str(exc_info.value)
        assert http_client.get.call_count == 5

    @pytest.mark.asyncio
    async def test_tolerates_transient_bare_404_before_success(self, mocker: MockerFixture) -> None:
        """A short blip of bare 404s that resolves to 200 must not raise."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.Mock())
        bare_404 = mocker.Mock(status_code=404, content=b"", headers={})
        healthy = mocker.Mock(status_code=200)
        self._mock_http_client(mocker, [bare_404, bare_404, healthy])

        result = await spawner._wait_ready_with_host("https://gw:443", "vh", timeout=60.0)

        assert result is True

    @pytest.mark.asyncio
    async def test_transport_error_resets_the_bare_404_streak(self, mocker: MockerFixture) -> None:
        """An httpx.HTTPError between bare 404s resets the streak, not just pauses it.

        2 bare 404s, then a transport error, then 5 more bare 404s: if the
        error didn't reset the streak, it would raise after only 3 more
        (2 + 3 = 5) instead of requiring 5 more (2 + error + 5 = 8 calls).
        """
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.Mock())
        bare_404 = mocker.Mock(status_code=404, content=b"", headers={})
        http_client = self._mock_http_client(
            mocker,
            [bare_404, bare_404, httpx.ConnectError("boom")] + [bare_404] * 5,
        )

        with pytest.raises(RuntimeError):
            await spawner._wait_ready_with_host("https://gw:443", "vh", timeout=60.0)

        assert http_client.get.call_count == 8

    @pytest.mark.asyncio
    async def test_404_with_content_type_is_not_treated_as_ingress_mismatch(
        self, mocker: MockerFixture
    ) -> None:
        """A real app-level 404 (has content-type) never triggers the diagnostic.

        Distinguishes a genuine 404 response from the sandbox's own app
        (which would have a content-type header, e.g. application/json)
        from the ingress's bare 404 with no body/headers at all.
        """
        import itertools

        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.Mock())
        app_404 = mocker.Mock(
            status_code=404,
            content=b'{"detail":"not found"}',
            headers={"content-type": "application/json"},
        )
        # asyncio.sleep is mocked to a no-op (see _mock_http_client), so the
        # while loop can spin far faster than wall-clock time -- an
        # infinite iterator avoids exhausting a finite response list
        # before the timeout check below actually trips.
        http_client = self._mock_http_client(mocker, itertools.repeat(app_404))

        result = await spawner._wait_ready_with_host("https://gw:443", "vh", timeout=0.05)

        assert result is False
        assert http_client.get.call_count >= 1


class TestEntrypointSpawnerFactory:
    """Tests for _create_spawner() auth configuration (#174)."""

    def _patch_spawner_type(self, mocker: MockerFixture) -> None:
        """Patch the module-level SPAWNER_TYPE constant."""
        import cloud_agents.workflow.executor.temporal.entrypoint as ep

        mocker.patch.object(ep, "SPAWNER_TYPE", "openshell")

    def test_openshell_no_auth(self, mocker: MockerFixture) -> None:
        """Creates OpenShellSpawner without auth by default."""
        self._patch_spawner_type(mocker)
        mocker.patch.dict(
            os.environ,
            {
                "OPENSHELL_GATEWAY_URL": "gw:17670",
            },
            clear=False,
        )

        mock_sandbox_client = mocker.patch("openshell.SandboxClient")

        from cloud_agents.workflow.executor.temporal.entrypoint import _create_spawner

        spawner = _create_spawner()

        mock_sandbox_client.assert_called_with(endpoint="gw:17670")
        assert spawner._endpoint == "gw:17670"
        assert spawner._http_endpoint == ""
        assert spawner._tls_ca == ""

    def test_openshell_mtls_auth(self, mocker: MockerFixture, tmp_path) -> None:
        """Creates OpenShellSpawner with mTLS when TLS env vars set."""
        self._patch_spawner_type(mocker)
        ca = tmp_path / "ca.pem"
        ca.write_text("ca")
        cert = tmp_path / "client.pem"
        cert.write_text("cert")
        key = tmp_path / "client.key"
        key.write_text("key")

        mocker.patch.dict(
            os.environ,
            {
                "OPENSHELL_GATEWAY_URL": "gw:17670",
                "OPENSHELL_TLS_CA": str(ca),
                "OPENSHELL_TLS_CERT": str(cert),
                "OPENSHELL_TLS_KEY": str(key),
            },
            clear=False,
        )

        mock_sandbox_client = mocker.patch("openshell.SandboxClient")
        mocker.patch("openshell.TlsConfig")

        from cloud_agents.workflow.executor.temporal.entrypoint import _create_spawner

        spawner = _create_spawner()

        call_kwargs = mock_sandbox_client.call_args.kwargs
        assert "tls" in call_kwargs
        assert spawner._tls_ca == str(ca)

    def test_openshell_bearer_token(self, mocker: MockerFixture) -> None:
        """Creates OpenShellSpawner with bearer token auth."""
        self._patch_spawner_type(mocker)
        mocker.patch.dict(
            os.environ,
            {
                "OPENSHELL_GATEWAY_URL": "gw:17670",
                "OPENSHELL_BEARER_TOKEN": "my-oidc-token",
            },
            clear=False,
        )

        mock_sandbox_client = mocker.patch("openshell.SandboxClient")

        from cloud_agents.workflow.executor.temporal.entrypoint import _create_spawner

        spawner = _create_spawner()

        call_kwargs = mock_sandbox_client.call_args.kwargs
        assert call_kwargs["bearer_token"] == "my-oidc-token"
        assert spawner._bearer_token == "my-oidc-token"

    def test_openshell_http_endpoint_override(self, mocker: MockerFixture) -> None:
        """OPENSHELL_HTTP_ENDPOINT is passed through to spawner."""
        self._patch_spawner_type(mocker)
        mocker.patch.dict(
            os.environ,
            {
                "OPENSHELL_GATEWAY_URL": "gw:17670",
                "OPENSHELL_HTTP_ENDPOINT": "https://proxy.example.com",
            },
            clear=False,
        )

        mocker.patch("openshell.SandboxClient")

        from cloud_agents.workflow.executor.temporal.entrypoint import _create_spawner

        spawner = _create_spawner()

        assert spawner._http_endpoint == "https://proxy.example.com"
