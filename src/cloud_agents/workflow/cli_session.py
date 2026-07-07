"""CLI session launcher for programmatic Claude Code sessions.

Launches interactive Claude Code sessions as sandbox containers through
the existing spawner infrastructure. Each session gets its own container
with scoped credentials and a mounted context file.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

from cloud_agents.workflow.audit import emit_audit

if TYPE_CHECKING:
    from cloud_agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)

DEFAULT_MAX_SESSION_SECONDS: int = 3600


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
