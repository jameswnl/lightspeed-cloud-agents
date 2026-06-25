"""Async step dispatcher for stateless workflow execution.

Dispatches workflow steps to ephemeral pods without blocking the
runner. Results come back via a trusted ingest API callback.
Ephemeral pods never get DB credentials.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from agents.remote_agent_client import RemoteAgentClient
from agents.spawner.base import AgentSpawner
from agents.workflow.definition import WorkflowStepSpec
from agents.workflow.state import StepResult

logger = logging.getLogger(__name__)


class StepDispatcher:
    """Dispatches workflow steps asynchronously.

    For pre-deployed agents: calls RemoteAgentClient directly (sync).
    For ephemeral agents: spawns pod with callback URL (async).

    Attributes:
        spawner: Agent spawner for ephemeral pods.
        agent_image: Container image for spawned agents.
        callback_base_url: Base URL for result callbacks.
    """

    def __init__(
        self,
        client_factory: Any,
        spawner: Optional[AgentSpawner] = None,
        agent_image: str = "agent-runtime:latest",
        callback_base_url: str = "",
    ) -> None:
        """Initialize the dispatcher.

        Args:
            client_factory: Factory for creating RemoteAgentClient.
            spawner: Optional spawner for ephemeral pods.
            agent_image: Container image for spawned agents.
            callback_base_url: Base URL for step result callbacks.
        """
        self._client_factory = client_factory
        self._spawner = spawner
        self._agent_image = agent_image
        self._callback_base_url = callback_base_url

    async def dispatch(
        self,
        step: WorkflowStepSpec,
        prompt: str,
        workflow_id: str,
        context: dict[str, Any] | None = None,
    ) -> StepResult:
        """Dispatch a step for execution.

        For pre-deployed agents, executes synchronously and returns
        the result. For ephemeral agents with a spawner, spawns the
        pod and executes synchronously (async dispatch with callback
        is a future enhancement — requires the result-ingest endpoint).

        Args:
            step: The step specification.
            prompt: The interpolated prompt.
            workflow_id: The workflow run ID.
            context: Optional request context.

        Returns:
            StepResult from the agent execution.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        step_id = str(uuid.uuid4())

        spawned_name = None
        try:
            if step.spawn in ("on-demand", "ephemeral") and self._spawner:
                spawn_id = uuid.uuid4().hex[:8]
                spawned_name = f"{step.agent}-{spawn_id}"
                endpoint = await self._spawner.spawn(
                    spawned_name, self._agent_image,
                    env={
                        "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
                        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://localhost:11434/v1"),
                        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
                    },
                    config=step.spawn_config,
                )
                await self._spawner.wait_ready(endpoint)
                client = RemoteAgentClient(endpoint)
            else:
                client = self._client_factory(step.agent)

            response = await client.run(prompt, context=context)
        except Exception as exc:
            return StepResult(
                step_name=step.name,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            if spawned_name and self._spawner:
                await self._spawner.destroy(spawned_name)

        return StepResult(
            step_name=step.name,
            status="completed",
            output=response.output,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    async def dispatch_async(
        self,
        step: WorkflowStepSpec,
        prompt: str,
        workflow_id: str,
        persistence: Any = None,
        attempt: int = 1,
        context: dict[str, Any] | None = None,
    ) -> StepResult:
        """Dispatch a step asynchronously (fire-and-forget).

        Persists 'dispatched' state before spawning (persist-before-spawn).
        Does NOT block on result. Does NOT destroy the pod.

        Args:
            step: The step specification.
            prompt: The interpolated prompt.
            workflow_id: The workflow run ID.
            persistence: Persistence backend for state writes.
            attempt: Current attempt number (1-indexed).
            context: Optional request context.

        Returns:
            StepResult with status="dispatched".
        """
        import hashlib

        started_at = datetime.now(timezone.utc).isoformat()

        hash_input = f"{workflow_id}:{step.name}:{attempt}"
        spawn_id = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
        spawned_name = f"{step.agent}-{spawn_id}"

        dispatched_result = StepResult(
            step_name=step.name,
            status="dispatched",
            started_at=started_at,
            output={
                "spawned_name": spawned_name,
                "run_id": None,
                "endpoint": None,
                "attempt": attempt,
            },
        )

        from agents.workflow.advancement import save_with_version, StaleStateError

        if persistence:
            state = await persistence.load(workflow_id)
            if state:
                state.steps[step.output_key] = dispatched_result
                state.status = "running"
                await save_with_version(persistence, state, state.version)

        if not self._spawner:
            raise RuntimeError("Async dispatch requires a spawner")

        callback_url = (
            f"{self._callback_base_url}/v1/workflows/{workflow_id}"
            f"/steps/{step.output_key}/result"
        )
        env = {
            "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
            "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://localhost:11434/v1"),
            "RESULT_CALLBACK_URL": callback_url,
            "RESULT_CALLBACK_ATTEMPT": str(attempt),
        }
        api_token = os.environ.get("AGENT_API_TOKEN", "")
        if api_token:
            env["AGENT_API_TOKEN"] = api_token

        endpoint = await self._spawner.spawn(
            spawned_name, self._agent_image, env=env, config=step.spawn_config,
            labels={
                "cloud-agents/workflow-id": workflow_id,
                "cloud-agents/step-name": step.name,
                "cloud-agents/attempt": str(attempt),
            },
        )
        await self._spawner.wait_ready(endpoint)

        from agents.runtime.auth import get_api_token
        client = RemoteAgentClient(endpoint, auth_token=get_api_token() or None)
        run_id = await client.run_async(prompt, context=context)

        dispatched_result.output["run_id"] = run_id
        dispatched_result.output["endpoint"] = endpoint

        if persistence:
            state = await persistence.load(workflow_id)
            if state:
                state.steps[step.output_key] = dispatched_result
                await save_with_version(persistence, state, state.version)

        return dispatched_result
