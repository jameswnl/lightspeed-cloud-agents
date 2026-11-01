"""Process-boundary tests for spawner/exec_transport.py.

The transport talks over a real localhost gRPC socket to
``tests/fixtures/fake_openshell_exec.py``, which runs each command as a real
subprocess. Channels come from a real ``OpenShellSpawner._create_grpc_channel``.

Tests marked ``stepcli`` run the real sandbox step CLI (lightspeed-agentic-sandbox
``tests.step_fakes``, which runs ``lightspeed_agentic.step.main`` with a fake
provider). They need ``LIGHTSPEED_SANDBOX_REPO`` (a checkout) and
``LIGHTSPEED_SANDBOX_PYTHON`` (its venv's python), and are skipped without them
unless ``STEPCLI_REQUIRED`` is set, as in CI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import sys
import threading
import time
import tracemalloc
from unittest import mock

import pytest

pytest.importorskip("openshell")

import grpc  # noqa: E402
from openshell._proto import openshell_pb2, openshell_pb2_grpc  # noqa: E402

from cloud_agents.spawner.exec_request import (  # noqa: E402
    GATEWAY_MAX_MESSAGE_BYTES,
    StepRequestTooLarge,
    build_step_request,
    check_exec_message_size,
)
from cloud_agents.spawner.exec_transport import (  # noqa: E402
    ExecCall,
    exec_capture,
    read_step_exec,
)
from cloud_agents.spawner.openshell_spawner import OpenShellSpawner  # noqa: E402
from cloud_agents.spawner.step_protocol import (  # noqa: E402
    MAX_RESULT_LINE,
    MAX_STDOUT_TOTAL,
    StepProtocolReader,
    map_outcome,
    needs_result_file,
)
from tests.fixtures.fake_openshell_exec import (  # noqa: E402
    FakeOpenShellGateway,
    inline_command,
)

SANDBOX_ID = "sbx-0001"
PROMPT = "Diagnose the issue: request-content-marker"
SANDBOX_REPO = os.environ.get("LIGHTSPEED_SANDBOX_REPO", "")
SANDBOX_PYTHON = os.environ.get("LIGHTSPEED_SANDBOX_PYTHON", "")

stepcli = [
    pytest.mark.stepcli,
    pytest.mark.skipif(
        not (SANDBOX_REPO and SANDBOX_PYTHON) and not os.environ.get("STEPCLI_REQUIRED"),
        reason="set LIGHTSPEED_SANDBOX_REPO and LIGHTSPEED_SANDBOX_PYTHON to run the step CLI",
    ),
]


@pytest.fixture
def gateway():
    fake = FakeOpenShellGateway()
    fake.target = fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def channel_factory(gateway):
    return OpenShellSpawner(endpoint=gateway.target)._create_grpc_channel


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


class TestExecCall:
    async def test_streams_output_and_exit_code(self, channel_factory):
        script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"
        chunks = []
        async with ExecCall(
            channel_factory, SANDBOX_ID, [sys.executable, "-c", script], timeout_seconds=30
        ) as call:
            async for stream, data in call:
                chunks.append((stream, data))
        assert b"".join(d for s, d in chunks if s == "stdout") == b"out\n"
        # Children forked from a gRPC process may print gRPC fork-handler lines first.
        assert b"".join(d for s, d in chunks if s == "stderr").endswith(b"err\n")
        assert call.exit_code == 7
        assert call.error is None

    async def test_request_shape_and_env_cleared(self, gateway, channel_factory, monkeypatch):
        monkeypatch.setenv("HOST_ONLY_SENTINEL", "leak")
        exit_code, stdout, _ = await exec_capture(
            channel_factory,
            SANDBOX_ID,
            ["sh", "-c", 'cat; echo; echo "pwd=$(pwd)"; env'],
            env={"GIVEN": "yes"},
            stdin=b"from-stdin",
            workdir="/tmp",
            timeout_seconds=30,
            max_stdout=64 * 1024,
        )
        assert exit_code == 0
        out = stdout.decode()
        assert out.startswith("from-stdin\n")
        assert "pwd=/tmp" in out or "pwd=/private/tmp" in out
        assert "GIVEN=yes" in out.splitlines()
        assert "HOST_ONLY_SENTINEL" not in out
        req = gateway.execs[0].request
        assert req.sandbox_id == SANDBOX_ID
        assert req.timeout_seconds == 30
        assert req.stdin == b"from-stdin"

    async def test_unknown_command_exits_127(self, channel_factory):
        """The gateway runs the command through a shell, so a missing binary is exit 127."""
        exit_code, stdout, stderr = await exec_capture(
            channel_factory,
            SANDBOX_ID,
            ["/nonexistent/binary"],
            timeout_seconds=30,
            max_stdout=1024,
        )
        assert exit_code == 127
        assert stdout == b""
        assert b"/nonexistent/binary" in stderr

    async def test_reader_exception_is_recorded_as_error(self):
        """A non-gRPC failure in the reader thread must not look like a clean end."""

        class BrokenCall:
            def __iter__(self):
                return self

            def __next__(self):
                raise ValueError("decode failed")

            def cancel(self):
                pass

        channel = mock.Mock()
        channel.unary_stream.return_value = lambda _request, timeout: BrokenCall()
        async with ExecCall(lambda: channel, SANDBOX_ID, ["true"], timeout_seconds=30) as call:
            assert [c async for c in call] == []
        assert call.exit_code is None
        assert isinstance(call.error, ValueError)
        channel.close.assert_called_once()

    async def test_cancel_during_channel_creation_closes_channel(self):
        release = threading.Event()
        channel = mock.Mock()

        def slow_factory():
            release.wait(10)
            return channel

        call = ExecCall(slow_factory, SANDBOX_ID, ["true"], timeout_seconds=30)
        task = asyncio.create_task(call.__aenter__())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await _wait_for(lambda: channel.close.called, timeout=5)
        channel.unary_stream.assert_not_called()

    def test_rejects_empty_command_and_missing_timeout(self, channel_factory):
        with pytest.raises(ValueError, match="command"):
            ExecCall(channel_factory, SANDBOX_ID, [], timeout_seconds=30)
        with pytest.raises(ValueError, match="timeout_seconds"):
            ExecCall(channel_factory, SANDBOX_ID, ["true"], timeout_seconds=0)


class TestRequestSize:
    @staticmethod
    def _stdin_for_size(size: int) -> bytes:
        """stdin that makes the ["true"] request exactly `size` bytes."""
        n = size
        while True:
            req = openshell_pb2.ExecSandboxRequest(
                sandbox_id=SANDBOX_ID,
                command=["true"],
                workdir="/",
                timeout_seconds=30,
                stdin=b"x" * n,
            )
            if (gap := size - req.ByteSize()) == 0:
                return req.stdin
            n += gap

    @staticmethod
    def _largest_allowed_stdin(env: dict[str, str], argv: list[str]) -> int:
        lo, hi = 0, GATEWAY_MAX_MESSAGE_BYTES
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                check_exec_message_size(b"x" * mid, env, argv, sandbox_id=SANDBOX_ID, workdir="/")
                lo = mid
            except StepRequestTooLarge:
                hi = mid - 1
        return lo

    async def test_over_limit_rejected_before_sending(self, gateway, channel_factory):
        env = {"GIVEN": "yes"}
        n = self._largest_allowed_stdin(env, ["true"]) + 1
        with pytest.raises(StepRequestTooLarge):
            ExecCall(
                channel_factory, SANDBOX_ID, ["true"], env=env, stdin=b"x" * n, timeout_seconds=30
            )
        assert gateway.execs == []

    async def test_largest_allowed_request_accepted_by_gateway(self, channel_factory):
        """check_exec_message_size is conservative: what it allows fits the 1 MiB cap."""
        env = {"GIVEN": "yes"}
        n = self._largest_allowed_stdin(env, ["true"])
        exit_code, _, _ = await exec_capture(
            channel_factory,
            SANDBOX_ID,
            ["true"],
            env=env,
            stdin=b"x" * n,
            timeout_seconds=30,
            max_stdout=1,
        )
        assert exit_code == 0

    def test_fake_gateway_enforces_1mib(self, gateway, channel_factory):
        """The cap is the gateway's: bypassing the transport check is refused server-side."""
        stdin = self._stdin_for_size(GATEWAY_MAX_MESSAGE_BYTES + 1)
        channel = channel_factory()
        try:
            stub = openshell_pb2_grpc.OpenShellStub(channel)
            req = openshell_pb2.ExecSandboxRequest(
                sandbox_id=SANDBOX_ID,
                command=["true"],
                workdir="/",
                timeout_seconds=30,
                stdin=stdin,
            )
            with pytest.raises(grpc.RpcError) as exc_info:
                list(stub.ExecSandbox(req, timeout=10))
        finally:
            channel.close()
        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert gateway.execs == []


class TestCancel:
    async def test_cancel_unblocks_reader_thread(self, gateway, channel_factory):
        call = ExecCall(channel_factory, SANDBOX_ID, ["sleep", "60"], timeout_seconds=120)

        async def consume():
            async with call:
                async for _ in call:
                    pass

        task = asyncio.create_task(consume())
        await _wait_for(lambda: gateway.execs and gateway.execs[0].proc is not None)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 2
        assert not call._thread.is_alive()
        assert call.exit_code is None
        await _wait_for(lambda: gateway.execs[0].disconnected)
        # Non-interactive exec: the process outlives the call until teardown.
        assert gateway.execs[0].proc.poll() is None

    async def test_aclose_wakes_a_concurrent_consumer(self, gateway, channel_factory):
        call = ExecCall(channel_factory, SANDBOX_ID, ["sleep", "30"], timeout_seconds=60)
        async with call:

            async def consume():
                return [c async for c in call]

            consumer = asyncio.create_task(consume())
            await _wait_for(lambda: gateway.execs and gateway.execs[0].proc is not None)
            await asyncio.sleep(0.1)  # consumer is waiting on the queue
            await call.aclose()
            done, _ = await asyncio.wait({consumer}, timeout=2)
            assert consumer in done, "consumer still waiting after aclose()"
            assert [s for s, _ in consumer.result() if s == "stdout"] == []
            # Iterating a closed call ends at once.
            assert await asyncio.wait_for(consume(), timeout=1) == []
        assert call.exit_code is None

    async def test_aclose_after_completion_is_noop(self, channel_factory):
        call = ExecCall(channel_factory, SANDBOX_ID, ["true"], timeout_seconds=30)
        async with call:
            assert [s async for s, _ in call if s == "stdout"] == []
        await call.aclose()
        assert call.exit_code == 0


class TestBoundedOutput:
    async def test_huge_stdout_aborts_reader_with_bounded_memory(self, channel_factory):
        event = json.dumps(
            {"v": 1, "type": "event", "event": {"ts": "t", "type": "thinking", "data": {}}}
        )
        total = MAX_STDOUT_TOTAL + 16 * 1024 * 1024
        script = (
            "import sys; w = sys.stdout.buffer.write; w(sys.argv[1].encode() + b'\\n'); "
            "line = b'x' * 1023 + b'\\n'\n"
            f"for _ in range({total // 1024}): w(line)"
        )
        reader = StepProtocolReader()
        events = []
        tracemalloc.start()
        try:
            async with ExecCall(
                channel_factory,
                SANDBOX_ID,
                [sys.executable, "-c", script, event],
                timeout_seconds=120,
            ) as call:
                exit_code = await read_step_exec(call, reader, on_event=events.append)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert reader.abort_reason == "output_too_large"
        assert exit_code is None
        assert len(events) == 1
        assert peak < 16 * 1024 * 1024

    async def test_exec_capture_caps_stdout(self, channel_factory):
        exit_code, stdout, _ = await exec_capture(
            channel_factory,
            SANDBOX_ID,
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'y' * 8_000_000)"],
            timeout_seconds=30,
            max_stdout=1000,
        )
        assert stdout == b"y" * 1000
        assert exit_code is None  # stopped reading at the cap


@pytest.mark.usefixtures("gateway")
class TestStepCLI:
    """The real sandbox step CLI as the exec'd process."""

    pytestmark = stepcli

    @pytest.fixture
    def run_root(self, tmp_path):
        root = tmp_path / "lsrun"
        root.mkdir(mode=0o700)
        return str(root)

    def _request(self, run_root: str, **kwargs):
        req = build_step_request(
            {"name": "transport test", "prompt": PROMPT},
            workflow_id="wf-exec-transport",
            attempt=1,
            context={"steps": {}},
            run_root=run_root,
            **kwargs,
        )
        os.mkdir(req.run_dir, 0o700)  # PR-D/E: the exec wrapper's mkdir
        return req

    def _env(self, run_root: str) -> dict[str, str]:
        return {"PYTHONPATH": SANDBOX_REPO, "STEP_FAKE_RUN_ROOT": run_root}

    def _argv(self, scenario: str, *cli_args: str) -> list[str]:
        return [SANDBOX_PYTHON, "-P", "-m", "tests.step_fakes", scenario, *cli_args]

    async def _run(self, channel_factory, argv, req, run_root, timeout_seconds=60):
        reader = StepProtocolReader()
        events = []
        async with ExecCall(
            channel_factory,
            SANDBOX_ID,
            argv,
            env=self._env(run_root),
            stdin=req.stdin,
            timeout_seconds=timeout_seconds,
        ) as call:
            exit_code = await read_step_exec(call, reader, on_event=events.append)
        return exit_code, reader, events

    async def test_ok(self, gateway, channel_factory, run_root):
        req = self._request(run_root)
        exit_code, reader, events = await self._run(
            channel_factory, self._argv("ok"), req, run_root
        )
        assert exit_code == 0
        assert [e.type for e in events] == [e.type for e in reader.events]
        assert "tool_call" in [e.type for e in events]
        outcome = map_outcome(exit_code, reader)
        assert (outcome.status, outcome.reason, outcome.result_source) == (
            "completed",
            "ok",
            "stdout",
        )
        assert outcome.body["rootCause"] == "oom"
        record = gateway.execs[0]
        assert PROMPT not in inline_command(record.request)
        assert record.request.stdin == req.stdin

    async def test_rejected_exit_2(self, channel_factory, run_root):
        req = self._request(run_root)
        stdin = (json.dumps({**json.loads(req.stdin), "allowedTools": ["Bash"]}) + "\n").encode()
        exit_code, reader, _ = await self._run(
            channel_factory, self._argv("ok"), dataclasses.replace(req, stdin=stdin), run_root
        )
        assert exit_code == 2
        outcome = map_outcome(exit_code, reader)
        assert (outcome.status, outcome.reason) == ("failed", "rejected")

    async def test_hard_deadline_exit_124(self, channel_factory, run_root):
        req = self._request(run_root, timeout_seconds=1)
        exit_code, reader, _ = await self._run(
            channel_factory, self._argv("stubborn", "--grace-seconds", "1"), req, run_root
        )
        assert exit_code == 124
        outcome = map_outcome(exit_code, reader)
        assert (outcome.status, outcome.reason) == ("error", "deadline")

    async def _read_result_file(self, channel_factory, run_dir: str):
        return await exec_capture(
            channel_factory,
            SANDBOX_ID,
            ["sh", "-c", 'cat -- "$1/result.json"', "sh", run_dir],
            timeout_seconds=15,
            max_stdout=MAX_RESULT_LINE + 1,
        )

    async def test_stdout_closed_exit_4_result_file_is_diagnostic_only(
        self, channel_factory, run_root
    ):
        """The CLI's stdout pipe is closed before it writes; result.json comes from a second exec."""
        req = self._request(run_root)
        close_stdout = (
            "import os, subprocess, sys; r, w = os.pipe(); os.close(r); "
            "sys.exit(subprocess.run(sys.argv[1:], stdout=w).returncode)"
        )
        argv = [SANDBOX_PYTHON, "-c", close_stdout, *self._argv("ok")]
        exit_code, reader, _ = await self._run(channel_factory, argv, req, run_root)
        assert exit_code == 4
        assert reader.result is None
        assert needs_result_file(exit_code, reader)

        read_exit, result_file, _ = await self._read_result_file(channel_factory, req.run_dir)
        assert read_exit == 0
        outcome = map_outcome(exit_code, reader, result_file)
        assert (outcome.status, outcome.reason, outcome.result_source) == (
            "failed",
            "result_unverified",
            "file",
        )
        assert outcome.taint_lease
        assert outcome.body is None
        assert outcome.diagnostic.startswith("untrusted result.json: status=200 success=True")
        assert "has_summary=True" in outcome.diagnostic
        assert "mock result" not in outcome.diagnostic  # no text from the file

    async def test_lost_transport_result_file_is_diagnostic_only(
        self, gateway, channel_factory, run_root
    ):
        """The call is cancelled before the CLI writes; it publishes result.json anyway."""
        req = self._request(run_root)
        reader = StepProtocolReader()
        call = ExecCall(
            channel_factory,
            SANDBOX_ID,
            self._argv("ok"),
            env=self._env(run_root),
            stdin=req.stdin,
            timeout_seconds=60,
        )

        async def run():
            async with call:
                await read_step_exec(call, reader)

        task = asyncio.create_task(run())
        await _wait_for(lambda: gateway.execs and gateway.execs[0].proc is not None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        reader.finish()
        assert call.exit_code is None
        # The gateway doesn't kill it: the CLI hits a closed stdout and still publishes.
        assert gateway.execs[0].proc.wait(timeout=30) == 4

        read_exit, result_file, _ = await self._read_result_file(channel_factory, req.run_dir)
        assert read_exit == 0
        outcome = map_outcome(call.exit_code, reader, result_file)
        assert (outcome.status, outcome.reason, outcome.result_source) == (
            "failed",
            "result_unverified",
            "file_after_transport_loss",
        )
        assert outcome.taint_lease

    async def test_cancel_mid_run_leaves_process_to_teardown(
        self, gateway, channel_factory, run_root
    ):
        req = self._request(run_root)
        pid_file = os.path.join(req.run_dir, "pid")
        reader = StepProtocolReader()
        call = ExecCall(
            channel_factory,
            SANDBOX_ID,
            self._argv("hang"),
            env=self._env(run_root),
            stdin=req.stdin,
            timeout_seconds=3600,
        )

        async def run():
            async with call:
                await read_step_exec(call, reader)

        task = asyncio.create_task(run())
        await _wait_for(lambda: os.path.exists(pid_file) and os.path.getsize(pid_file) > 0)
        pid = int(open(pid_file).read())
        try:
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - started < 2
            assert not call._thread.is_alive()
            assert call.exit_code is None
            await _wait_for(lambda: gateway.execs[0].disconnected)
            assert _alive(pid), "the gateway must not kill the process on disconnect"
            assert map_outcome(call.exit_code, reader).reason == "no_result"
        finally:
            os.killpg(pid, signal.SIGKILL)  # the sandbox teardown's job in production
