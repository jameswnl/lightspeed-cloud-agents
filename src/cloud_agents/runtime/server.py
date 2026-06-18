"""Agent runtime HTTP server.

Provides a FastAPI application with /v1/run and /healthz endpoints
for running Pydantic AI agents in agent pods.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException

from agents.models import AgentRunRequest, AgentRunResponse

AgentRunner = Callable[[AgentRunRequest], Awaitable[AgentRunResponse]]


def create_app(
    *,
    agent_runner: AgentRunner,
    agent_name: str,
) -> FastAPI:
    """Create a FastAPI app for an agent pod.

    Args:
        agent_runner: Async callable that processes an AgentRunRequest
            and returns an AgentRunResponse.
        agent_name: Identifier for this agent (included in health and responses).

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title=f"Agent: {agent_name}")
    app.state.agent_runner = agent_runner
    app.state.agent_name = agent_name

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Readiness check."""
        return {"status": "ready", "agent_name": agent_name}

    @app.post("/v1/run")
    async def run(body: AgentRunRequest) -> AgentRunResponse:
        """Run the agent with the given prompt."""
        try:
            return await app.state.agent_runner(body)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Agent run failed: {type(exc).__name__}: {exc}",
            ) from exc

    return app
