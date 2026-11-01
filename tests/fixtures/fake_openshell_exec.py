"""A fake OpenShell gateway that implements only ExecSandbox, for exec transport tests.

A real localhost gRPC server built from the openshell 0.0.111 wheel's
``openshell_pb2_grpc``. Each exec runs its command as a real host subprocess. It
emulates the gateway behaviour the exec transport depends on:

- The command is inlined as sorted ``ENV=val ... argv`` and run through
  ``/bin/sh -c``, as the gateway runs it through the sandbox user's login shell.
  So the request's ``environment`` reaches the child only via those inline
  assignments, and a missing binary is the shell's exit 127 with a message on
  stderr, not an RPC error.
- The shell's own env is cleared and rebuilt from a small base, like the
  supervisor's ``env_clear()`` (see ``OpenShellSpawner._DEFAULT_EXTRA_ENV``).
  The base here is a stand-in, not the supervisor's real allowlist.
- Inbound messages are capped at 1 MiB, the whole ``ExecSandboxRequest``
  including stdin. The inlined command string is capped at 256 KiB.
- The first 120 characters of the inlined command are logged and kept as
  ``ExecRecord.preview``. The quoting is approximate.
- stdin is written to the child and closed. stdout and stderr are streamed as
  they arrive, then one exit event (a signal becomes 128 + n).
- Non-interactive disconnect: when the client goes away, the server stops
  streaming and closes its ends of the child's stdout/stderr pipes, but never
  signals the child. Only ``stop()`` (test teardown) kills leftovers.

Children are forked from this gRPC process, so their stderr may start with gRPC
fork-handler log lines, much as a real exec's stderr carries login-shell noise.

Not emulated: login-shell startup files (``-l``), Landlock, ``timeout_seconds`` enforcement (it is
only recorded), and provider placeholders.
"""

from __future__ import annotations

import logging
import os
import select
import shlex
import signal
import subprocess
import threading
from concurrent import futures
from dataclasses import dataclass

import grpc
from openshell._proto import openshell_pb2, openshell_pb2_grpc

logger = logging.getLogger(__name__)

GATEWAY_MAX_MESSAGE_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 256 * 1024
PREVIEW_CHARS = 120
_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.05


@dataclass
class ExecRecord:
    request: openshell_pb2.ExecSandboxRequest
    preview: str
    proc: subprocess.Popen | None = None
    disconnected: bool = False


def inline_command(request: openshell_pb2.ExecSandboxRequest) -> str:
    env = [f"{k}={shlex.quote(v)}" for k, v in sorted(request.environment.items())]
    return " ".join([*env, *(shlex.quote(arg) for arg in request.command)])


class FakeOpenShellGateway(openshell_pb2_grpc.OpenShellServicer):
    def __init__(self) -> None:
        self.execs: list[ExecRecord] = []
        self._server: grpc.Server | None = None

    def start(self) -> str:
        """Start serving on an ephemeral localhost port; return the host:port target."""
        self._server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=8),
            options=[("grpc.max_receive_message_length", GATEWAY_MAX_MESSAGE_BYTES)],
        )
        openshell_pb2_grpc.add_OpenShellServicer_to_server(self, self._server)
        port = self._server.add_insecure_port("127.0.0.1:0")
        self._server.start()
        return f"127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=None)
        for record in self.execs:
            if record.proc is not None and record.proc.poll() is None:
                _kill(record.proc)

    def ExecSandbox(self, request, context):  # noqa: N802 - gRPC method name
        command = inline_command(request)
        record = ExecRecord(request=request, preview=command[:PREVIEW_CHARS])
        self.execs.append(record)
        logger.info("exec sandbox_id=%s command=%s", request.sandbox_id, record.preview)
        if not request.command:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "command is required")
        if len(command.encode()) > MAX_COMMAND_BYTES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "command exceeds 256 KiB")

        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.path.expanduser("~")}
        proc = subprocess.Popen(  # noqa: S603 - test harness runs the requested command
            ["/bin/sh", "-c", command],
            env=base,
            cwd=request.workdir or "/",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # so stop() can kill the shell and its children
        )
        record.proc = proc
        threading.Thread(target=_write_stdin, args=(proc, request.stdin), daemon=True).start()

        names = {proc.stdout.fileno(): "stdout", proc.stderr.fileno(): "stderr"}
        try:
            while names:
                if not context.is_active():
                    record.disconnected = True
                    return
                ready, _, _ = select.select(list(names), [], [], _POLL_SECONDS)
                for fd in ready:
                    data = os.read(fd, _CHUNK_BYTES)
                    if not data:
                        del names[fd]
                    elif names[fd] == "stdout":
                        yield openshell_pb2.ExecSandboxEvent(
                            stdout=openshell_pb2.ExecSandboxStdout(data=data)
                        )
                    else:
                        yield openshell_pb2.ExecSandboxEvent(
                            stderr=openshell_pb2.ExecSandboxStderr(data=data)
                        )
            while (code := proc.poll()) is None:
                if not context.is_active():
                    record.disconnected = True
                    return
                threading.Event().wait(_POLL_SECONDS)
            yield openshell_pb2.ExecSandboxEvent(
                exit=openshell_pb2.ExecSandboxExit(exit_code=code if code >= 0 else 128 - code)
            )
        except GeneratorExit:  # the server dropped the stream: client cancel or deadline
            record.disconnected = True
            raise
        finally:
            if record.disconnected:
                proc.stdout.close()
                proc.stderr.close()


def _write_stdin(proc: subprocess.Popen, data: bytes) -> None:
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # gone, or (macOS) only a zombie left
        pass
    proc.wait()
