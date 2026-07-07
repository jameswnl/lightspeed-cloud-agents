"""CLI session launcher for programmatic Claude Code sessions.

Launches interactive Claude Code sessions as sandbox containers through
the existing spawner infrastructure. Each session gets its own container
with scoped credentials and a mounted context file.

Supports bi-directional communication: output monitoring via polling
with byte offset tracking, and message injection via JSONL file writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, AsyncIterator, Optional

from pydantic import BaseModel

from cloud_agents.workflow.audit import emit_audit

if TYPE_CHECKING:
    from cloud_agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)

DEFAULT_MAX_SESSION_SECONDS: int = 3600
DEFAULT_CHECK_INTERVAL: float = 30.0
DEFAULT_POLL_INTERVAL: float = 2.0

# Path inside the container for message exchange
_MESSAGE_FILE_PATH = "/var/run/cli-session/messages.jsonl"
# Path inside the container for session output
_OUTPUT_FILE_PATH = "/var/log/agent-events.jsonl"


class CLISessionStatus(str, Enum):
    """Lifecycle status of a CLI session.

    Attributes:
        RUNNING: Session container is active.
        COMPLETED: Session finished normally.
        FAILED: Session failed to start or errored.
        TERMINATED: Session was explicitly terminated.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"


class SessionOutputEvent(BaseModel):
    """A chunk of output from a CLI session.

    Attributes:
        event_type: Type of event (e.g. "output", "error", "done").
        data: The output data content.
        offset: Byte offset in the output file after this event.
    """

    event_type: str
    data: str
    offset: int


class CLISessionInfo(BaseModel):
    """Tracked metadata for a CLI session.

    Attributes:
        session_id: Unique session identifier.
        agent_name: Spawner agent name for the container.
        workflow_id: Associated workflow execution ID.
        started_at: ISO 8601 timestamp of session start.
        status: Current lifecycle status.
        endpoint: Container endpoint URL, if available.
        error: Error message if session failed.
    """

    session_id: str
    agent_name: str
    workflow_id: str
    started_at: str
    status: CLISessionStatus
    endpoint: Optional[str] = None
    error: Optional[str] = None


class CLISessionLauncher:
    """Launches and tracks CLI sessions via the spawner.

    Wraps ``AgentSpawner.spawn()`` with a Claude Code entrypoint
    instead of the default uvicorn server. Sessions are tracked in
    memory with session ID, workflow association, and lifecycle status.

    Attributes:
        max_session_seconds: Maximum session duration before auto-terminate.
    """

    def __init__(self, max_session_seconds: int = DEFAULT_MAX_SESSION_SECONDS) -> None:
        """Initialize the CLI session launcher.

        Args:
            max_session_seconds: Maximum session duration in seconds.
        """
        self.max_session_seconds = max_session_seconds
        self._sessions: dict[str, CLISessionInfo] = {}
        self._timeout_task: asyncio.Task[None] | None = None
        self._check_interval: float = DEFAULT_CHECK_INTERVAL

    async def launch(
        self,
        spawner: AgentSpawner,
        context_markdown: str,
        prompt: str,
        image: str,
        workflow_id: str,
        env: dict[str, str] | None = None,
        service_account: str | None = None,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
    ) -> str:
        """Launch a CLI session as a sandbox container.

        Spawns a new container through the provided spawner with a
        Claude Code entrypoint. The context markdown is passed via
        environment variable and the prompt via the entrypoint command.

        Parameters:
            spawner: AgentSpawner instance for container creation.
            context_markdown: Markdown context document for the session.
            prompt: The prompt to pass to claude -p.
            image: Container image to use for the session.
            workflow_id: Associated workflow execution ID.
            env: Optional environment variables for credential scoping.
            service_account: Optional K8s ServiceAccount override.
            credential_secret_name: Optional K8s Secret for credentials.
            mcp_secret_mounts: Optional MCP server secret mounts.

        Returns:
            Session ID string for tracking.

        Raises:
            RuntimeError: If the spawner fails to create the container.
        """
        session_id = f"cli-sess-{uuid.uuid4().hex[:12]}"
        agent_name = f"cli-{uuid.uuid4().hex[:12]}"

        session_env = dict(env or {})
        session_env["CLI_HANDOFF_CONTEXT"] = context_markdown
        session_env["CLI_HANDOFF_PROMPT"] = prompt

        labels = {
            "cloud-agents/session-type": "cli-handoff",
            "cloud-agents/workflow-id": workflow_id,
            "cloud-agents/session-id": session_id,
        }

        try:
            endpoint = await spawner.spawn(
                agent_name=agent_name,
                image=image,
                env=session_env,
                labels=labels,
                service_account=service_account,
                credential_secret_name=credential_secret_name,
                mcp_secret_mounts=mcp_secret_mounts,
            )

            info = CLISessionInfo(
                session_id=session_id,
                agent_name=agent_name,
                workflow_id=workflow_id,
                started_at=datetime.now(tz=UTC).isoformat(),
                status=CLISessionStatus.RUNNING,
                endpoint=endpoint,
            )
            self._sessions[session_id] = info

            emit_audit(
                event_type="cli_session_launched",
                workflow_id=workflow_id,
                details={
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "image": image,
                },
            )

            logger.info(
                "CLI session launched: session_id=%s, agent=%s, workflow=%s",
                session_id,
                agent_name,
                workflow_id,
            )

            return session_id

        except Exception as exc:
            emit_audit(
                event_type="cli_session_failed",
                workflow_id=workflow_id,
                details={
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "error": str(exc),
                },
            )
            raise

    def get_status(self, session_id: str) -> CLISessionInfo | None:
        """Get the current status of a CLI session.

        Parameters:
            session_id: The session identifier to look up.

        Returns:
            CLISessionInfo if found, None otherwise.
        """
        return self._sessions.get(session_id)

    async def terminate(self, session_id: str, spawner: AgentSpawner) -> None:
        """Terminate a running CLI session.

        Calls ``spawner.destroy()`` on the session's container and
        updates the tracked status.

        Parameters:
            session_id: The session identifier to terminate.
            spawner: AgentSpawner instance for container destruction.

        Raises:
            KeyError: If the session ID is not found.
            RuntimeError: If the spawner fails to destroy the container.
        """
        info = self._sessions.get(session_id)
        if info is None:
            raise KeyError(f"CLI session '{session_id}' not found")

        try:
            await spawner.destroy(info.agent_name)
            info.status = CLISessionStatus.TERMINATED

            emit_audit(
                event_type="cli_session_terminated",
                workflow_id=info.workflow_id,
                details={
                    "session_id": session_id,
                    "agent_name": info.agent_name,
                    "reason": "user_request",
                },
            )

            logger.info(
                "CLI session terminated: session_id=%s, agent=%s",
                session_id,
                info.agent_name,
            )

        except Exception as exc:
            info.status = CLISessionStatus.FAILED
            info.error = str(exc)
            logger.error(
                "CLI session terminate failed: session_id=%s, error=%s",
                session_id,
                exc,
            )
            raise

    def list_sessions(
        self, workflow_id: str | None = None
    ) -> list[CLISessionInfo]:
        """List all tracked CLI sessions.

        Parameters:
            workflow_id: Optional filter by workflow ID.

        Returns:
            List of CLISessionInfo for all matching sessions.
        """
        sessions = list(self._sessions.values())
        if workflow_id is not None:
            sessions = [s for s in sessions if s.workflow_id == workflow_id]
        return sessions

    async def monitor_output(
        self,
        session_id: str,
        spawner: AgentSpawner,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> AsyncIterator[SessionOutputEvent]:
        """Monitor session output via polling with byte offset tracking.

        Polls the session's output file at the given interval, yielding
        new content as ``SessionOutputEvent`` objects. Stops when the
        session is no longer RUNNING or when the output file disappears.

        Parameters:
            session_id: The session identifier to monitor.
            spawner: AgentSpawner for reading container files.
            poll_interval: Seconds between poll attempts (default 2.0).

        Yields:
            SessionOutputEvent for each new chunk of output.

        Raises:
            KeyError: If the session ID is not found.
        """
        info = self._sessions.get(session_id)
        if info is None:
            raise KeyError(f"CLI session '{session_id}' not found")

        offset = 0

        while True:
            # Check if session is still running
            current = self._sessions.get(session_id)
            if current is None or current.status != CLISessionStatus.RUNNING:
                break

            try:
                content = await spawner.read_file(info.agent_name, _OUTPUT_FILE_PATH)
                content_bytes = len(content.encode())

                if content_bytes > offset:
                    # Extract only new content
                    new_data = content.encode()[offset:].decode()
                    yield SessionOutputEvent(
                        event_type="output",
                        data=new_data,
                        offset=content_bytes,
                    )
                    offset = content_bytes

            except FileNotFoundError:
                # File doesn't exist yet or session ended
                break
            except Exception as exc:
                logger.warning(
                    "Output monitor error for session '%s': %s",
                    session_id,
                    exc,
                )
                break

            await asyncio.sleep(poll_interval)

    async def send_message(
        self,
        session_id: str,
        spawner: AgentSpawner,
        message: str,
    ) -> None:
        """Send a message to a running CLI session.

        Writes a JSONL line to the session's message file inside the
        container. The agent is expected to read from this file.

        Parameters:
            session_id: The session identifier to message.
            spawner: AgentSpawner for writing container files.
            message: The message text to send.

        Raises:
            KeyError: If the session ID is not found.
            RuntimeError: If the write operation fails.
        """
        info = self._sessions.get(session_id)
        if info is None:
            raise KeyError(f"CLI session '{session_id}' not found")

        msg_line = json.dumps({
            "message": message,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        })

        # Read existing content, append new message.
        # Note: read-then-write is non-atomic. Acceptable for current
        # single-writer design; revisit if concurrent senders are needed.
        try:
            existing = await spawner.read_file(info.agent_name, _MESSAGE_FILE_PATH)
        except FileNotFoundError:
            existing = ""

        new_content = existing + msg_line + "\n"
        await spawner.write_file(info.agent_name, _MESSAGE_FILE_PATH, new_content)

        emit_audit(
            event_type="cli_session_message_sent",
            workflow_id=info.workflow_id,
            details={
                "session_id": session_id,
                "agent_name": info.agent_name,
                "message_length": len(message),
            },
        )

        logger.info(
            "Message sent to CLI session: session_id=%s, length=%d",
            session_id,
            len(message),
        )

    def start_timeout_monitor(self, spawner: AgentSpawner) -> None:
        """Start the background timeout enforcement task.

        Launches an asyncio task that periodically checks all running
        sessions and terminates those exceeding ``max_session_seconds``.

        Parameters:
            spawner: AgentSpawner instance for container destruction.
        """
        self._timeout_task = asyncio.create_task(self._timeout_loop(spawner))
        logger.info(
            "Timeout monitor started: check_interval=%.1fs, max_session_seconds=%d",
            self._check_interval,
            self.max_session_seconds,
        )

    async def _timeout_loop(self, spawner: AgentSpawner) -> None:
        """Background loop that enforces session timeouts.

        Iterates over all tracked sessions, comparing their age against
        ``max_session_seconds``. Expired RUNNING sessions are destroyed
        via the spawner and marked TERMINATED with a ``timeout`` reason
        audit event.

        Parameters:
            spawner: AgentSpawner instance for container destruction.
        """
        try:
            while True:
                await asyncio.sleep(self._check_interval)
                now = datetime.now(tz=UTC)

                for session_id, info in list(self._sessions.items()):
                    if info.status != CLISessionStatus.RUNNING:
                        continue

                    started = datetime.fromisoformat(info.started_at)
                    elapsed = (now - started).total_seconds()

                    if elapsed <= self.max_session_seconds:
                        continue

                    logger.info(
                        "Session timeout: session_id=%s, elapsed=%.0fs, max=%ds",
                        session_id,
                        elapsed,
                        self.max_session_seconds,
                    )

                    try:
                        await spawner.destroy(info.agent_name)
                        info.status = CLISessionStatus.TERMINATED

                        emit_audit(
                            event_type="cli_session_terminated",
                            workflow_id=info.workflow_id,
                            details={
                                "session_id": session_id,
                                "agent_name": info.agent_name,
                                "reason": "timeout",
                            },
                        )
                    except Exception as exc:
                        info.status = CLISessionStatus.FAILED
                        info.error = str(exc)
                        logger.error(
                            "Timeout destroy failed: session_id=%s, error=%s",
                            session_id,
                            exc,
                        )

        except asyncio.CancelledError:
            logger.info("Timeout monitor cancelled")

    async def shutdown(self) -> None:
        """Cancel the background timeout monitor task.

        Safe to call multiple times or when no monitor is running.
        """
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass
            self._timeout_task = None
            logger.info("Timeout monitor shut down")
