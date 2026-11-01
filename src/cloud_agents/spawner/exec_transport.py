"""Async exec transport over the OpenShell gateway's raw ``ExecSandbox`` RPC.

Long-running step execs use this instead of ``SandboxClient.exec_stream()``
(openshell 0.0.111 ``sandbox.py:633-685``), because exec_stream():

- keeps every stdout/stderr chunk to build its final ``ExecResult``
  (``sandbox.py:661-684``), so memory grows with the exec's output;
- holds the gRPC call in a generator local (``sandbox.py:659``). Iterated in a
  worker thread, the generator is "already executing" while it waits for the
  next event, so nothing on the event loop can close it or cancel the call. A
  cancelled ``asyncio.to_thread`` leaves that thread blocked until the exec ends
  or its gRPC deadline passes.

:class:`ExecCall` keeps the call object, so :meth:`ExecCall.aclose` cancels the
RPC and the reader thread unblocks. Output is handed over through a bounded
queue, so a slow consumer applies backpressure instead of buffering.

Request content and secrets belong in stdin only: the gateway inlines env and
argv into one command string and logs its first 120 characters.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import grpc
from openshell._proto import openshell_pb2, openshell_pb2_grpc

from cloud_agents.spawner.exec_request import check_exec_message_size
from cloud_agents.spawner.lifetime import (
    SANDBOX_CANCEL_GRACE_SECONDS,
    SandboxDeadlineExceeded,
    await_within,
)
from cloud_agents.spawner.step_protocol import MAX_STDERR_TAIL, StepProtocolReader
from cloud_agents.workflow.core.models import TranscriptEvent

logger = logging.getLogger(__name__)

# Same headroom as the SDK adds over timeout_seconds for SSH setup and teardown.
GRPC_DEADLINE_HEADROOM_SECONDS = 10
_MAX_QUEUED_CHUNKS = 16
_EOF = object()


class ExecCall:
    """One streamed ``ExecSandbox`` call.

    Usage::

        async with ExecCall(spawner._create_grpc_channel, sandbox_id, argv,
                            stdin=data, timeout_seconds=t) as call:
            async for stream, data in call:
                ...
        call.exit_code  # None if the stream ended without an exit event
        call.error      # the gRPC error that ended the stream, if any

    Leaving the block, including by exception or task cancellation, cancels the
    call. That does not stop the process: a non-interactive exec outlives its
    client, so the caller must kill it or destroy the sandbox.

    Parameters:
        channel_factory: Returns a new gRPC channel to the gateway, normally
            ``OpenShellSpawner._create_grpc_channel`` so TLS and bearer auth are
            the spawner's. Called once per call, in a worker thread; the
            channel is closed with the call.
        sandbox_id: Sandbox UUID.
        command: argv. Identifiers only, never request content or secrets.
        timeout_seconds: Sent to the gateway as the exec timeout; the gRPC
            deadline adds GRPC_DEADLINE_HEADROOM_SECONDS. Required, since 0
            would mean no exec timeout.
        workdir: Working directory inside the sandbox.
        env: Extra env for the exec. Inlined into the gateway's logged
            command string, so never secrets.
        stdin: Written to the process's stdin, which is then closed.

    Raises:
        StepRequestTooLarge: The request may exceed the gateway's message cap
            (``check_exec_message_size``); nothing is sent.
    """

    def __init__(
        self,
        channel_factory: Callable[[], grpc.Channel],
        sandbox_id: str,
        command: Sequence[str],
        *,
        timeout_seconds: int,
        workdir: str = "/",
        env: Mapping[str, str] | None = None,
        stdin: bytes = b"",
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        check_exec_message_size(stdin, dict(env or {}), list(command))
        self._request = openshell_pb2.ExecSandboxRequest(
            sandbox_id=sandbox_id,
            command=list(command),
            workdir=workdir,
            environment=dict(env or {}),
            timeout_seconds=timeout_seconds,
            stdin=stdin,
        )
        self._channel_factory = channel_factory
        self._deadline = timeout_seconds + GRPC_DEADLINE_HEADROOM_SECONDS
        self.exit_code: int | None = None
        self.error: grpc.RpcError | None = None
        self._channel: grpc.Channel | None = None
        self._call: Any = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pump_done: asyncio.Future[None] | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        # One credit per queued item: the reader thread blocks when the
        # consumer falls _MAX_QUEUED_CHUNKS behind.
        self._credits = threading.Semaphore(_MAX_QUEUED_CHUNKS)
        self._closed = False
        self._finished = False

    async def __aenter__(self) -> ExecCall:
        self._loop = asyncio.get_running_loop()
        self._pump_done = self._loop.create_future()
        self._channel = await asyncio.to_thread(self._channel_factory)
        try:
            stub = openshell_pb2_grpc.OpenShellStub(self._channel)
            self._call = stub.ExecSandbox(self._request, timeout=self._deadline)
        except BaseException:
            self._channel.close()
            raise
        self._thread = threading.Thread(target=self._pump, name="openshell-exec", daemon=True)
        self._thread.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[tuple[str, bytes]]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[tuple[str, bytes]]:
        while not self._finished:
            item = await self._queue.get()
            self._credits.release()
            if item is _EOF:
                self._finished = True
            elif isinstance(item, grpc.RpcError):
                self.error = item
            elif item[0] == "exit":
                self.exit_code = item[1]
            else:
                yield item

    async def aclose(self) -> None:
        """Cancel the call, wait briefly for the reader thread, close the channel."""
        if self._closed:
            return
        self._closed = True
        if self._call is not None:
            self._call.cancel()
        self._credits.release(_MAX_QUEUED_CHUNKS + 1)  # unblock a waiting _put()
        if self._pump_done is not None:
            try:
                await await_within(
                    self._pump_done, SANDBOX_CANCEL_GRACE_SECONDS, what="exec reader thread"
                )
            except SandboxDeadlineExceeded:
                logger.warning("exec reader thread still running after cancel; abandoning it")
        if self._channel is not None:
            self._channel.close()

    def _pump(self) -> None:
        """Reader thread: move gRPC events to the queue until EOF, error or close."""
        try:
            for event in self._call:
                kind = event.WhichOneof("payload")
                if kind == "exit":
                    item: Any = ("exit", event.exit.exit_code)
                elif kind in ("stdout", "stderr"):
                    item = (kind, bytes(getattr(event, kind).data))
                else:
                    continue
                if not self._put(item):
                    return
        except grpc.RpcError as exc:
            self._put(exc)
        finally:
            self._put(_EOF)
            try:
                self._loop.call_soon_threadsafe(self._set_pump_done)
            except RuntimeError:  # event loop closed
                pass

    def _set_pump_done(self) -> None:
        if not self._pump_done.done():  # await_within cancels it on timeout
            self._pump_done.set_result(None)

    def _put(self, item: object) -> bool:
        self._credits.acquire()
        if self._closed:
            return False
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # event loop closed
            return False
        return True


async def exec_capture(
    channel_factory: Callable[[], grpc.Channel],
    sandbox_id: str,
    command: Sequence[str],
    *,
    timeout_seconds: int,
    max_stdout: int,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    workdir: str = "/",
) -> tuple[int | None, bytes, bytes]:
    """Run a short exec and return ``(exit_code, stdout, stderr_tail)``.

    stdout is kept up to ``max_stdout`` bytes; at the cap the call is cancelled
    and ``exit_code`` is None, so pass one more than the largest valid output to
    detect oversize. stderr keeps its last MAX_STDERR_TAIL bytes.
    """
    stdout = bytearray()
    stderr = bytearray()
    async with ExecCall(
        channel_factory,
        sandbox_id,
        command,
        timeout_seconds=timeout_seconds,
        workdir=workdir,
        env=env,
        stdin=stdin,
    ) as call:
        async for stream, data in call:
            if stream == "stderr":
                stderr += data
                del stderr[:-MAX_STDERR_TAIL]
                continue
            stdout += data[: max_stdout - len(stdout)]
            if len(stdout) >= max_stdout:
                break
    return call.exit_code, bytes(stdout), bytes(stderr)


async def read_step_exec(
    call: ExecCall,
    reader: StepProtocolReader,
    on_event: Callable[[TranscriptEvent], None] | None = None,
) -> int | None:
    """Feed a step exec's output to ``reader`` until it ends; return its exit code.

    Calls ``on_event`` for each transcript event as it is parsed. Stops early,
    cancelling the call, when the reader aborts (``output_too_large``,
    ``preprotocol_noise``); the exit code is then None.
    """
    async for stream, data in call:
        for event in reader.feed(stream, data):
            if on_event is not None:
                on_event(event)
        if reader.abort_reason:
            break
    if reader.abort_reason:
        await call.aclose()
    reader.finish()
    return call.exit_code
