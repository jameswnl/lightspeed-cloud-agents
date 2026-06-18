"""Generic agent runtime entrypoint.

Reads agent.yaml, loads tools, builds the Pydantic AI agent,
and starts the FastAPI server. Supports both request-response
and periodic-loop lifecycles.

Usage: python -m agents.runtime.generic_entrypoint
       (or via uvicorn: uvicorn agents.runtime.generic_entrypoint:app)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import yaml
from fastapi import FastAPI

from agents.definition import AgentDefinition
from agents.registry import AgentRegistry
from agents.remote_agent_client import RemoteAgentClient
from agents.runtime.generic_runner import create_generic_runner
from agents.runtime.model_factory import get_model, reset_model
from agents.runtime.server import create_app

logger = logging.getLogger(__name__)

AGENT_DEFINITION_PATH = os.environ.get("AGENT_DEFINITION", "/app/agent.yaml")
AGENT_REGISTRY_PATH = os.environ.get("AGENT_REGISTRY", "/app/registry.yaml")


def load_definition(path: str = AGENT_DEFINITION_PATH) -> AgentDefinition:
    """Load an AgentDefinition from a YAML file.

    Args:
        path: Path to the agent.yaml file.

    Returns:
        Parsed AgentDefinition.

    Raises:
        RuntimeError: If the file is missing or invalid.
    """
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"Agent definition not found: {path}")
    with open(p) as f:
        data = yaml.safe_load(f)
    return AgentDefinition.model_validate(data)


def load_registry(path: str = AGENT_REGISTRY_PATH) -> AgentRegistry:
    """Load agent registry from a YAML file.

    Args:
        path: Path to registry.yaml.

    Returns:
        AgentRegistry instance (empty if file not found).
    """
    p = Path(path)
    if not p.exists():
        logger.info("No registry file at %s, dispatch disabled", path)
        return AgentRegistry({})
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    agents = {a["name"]: a["endpoint"] for a in data.get("agents", [])}
    return AgentRegistry(agents)


def build_app(
    definition_path: str = AGENT_DEFINITION_PATH,
    registry_path: str = AGENT_REGISTRY_PATH,
) -> FastAPI:
    """Build the FastAPI app from an agent definition.

    Args:
        definition_path: Path to agent.yaml.
        registry_path: Path to registry.yaml.

    Returns:
        Configured FastAPI application.
    """
    defn = load_definition(definition_path)
    agent_name = defn.metadata["name"]
    spec = defn.spec

    # Run optional bootstrap hook from environment.
    # This replaces the hardcoded cluster_state initialization.
    # Agents that need domain-specific setup configure it via:
    #   AGENT_BOOTSTRAP_MODULE=agents.diagnostic.cluster_state
    #   AGENT_BOOTSTRAP_FUNCTION=init_scenario
    #   AGENT_BOOTSTRAP_ARGS=bad_deploy
    bootstrap_module = os.environ.get("AGENT_BOOTSTRAP_MODULE")
    bootstrap_function = os.environ.get("AGENT_BOOTSTRAP_FUNCTION")
    if bootstrap_module and bootstrap_function:
        import importlib

        mod = importlib.import_module(bootstrap_module)
        fn = getattr(mod, bootstrap_function)
        bootstrap_args = os.environ.get("AGENT_BOOTSTRAP_ARGS", "")
        if bootstrap_args:
            fn(bootstrap_args)
        else:
            fn()

    reset_model()
    model_override = spec.model or {}
    model = get_model(
        model_name=model_override.get("name"),
        base_url=model_override.get("base_url"),
        api_key=model_override.get("api_key"),
    )

    timeout = spec.resources.timeout_seconds if spec.resources else 600
    runner = create_generic_runner(spec, model, agent_name)
    base_app = create_app(
        agent_runner=runner,
        agent_name=agent_name,
        run_timeout=float(timeout),
    )

    if spec.lifecycle.type == "periodic-loop" and spec.lifecycle.dispatch_to:
        registry = load_registry(registry_path)
        _attach_loop(base_app, spec, runner, registry, agent_name)

    return base_app


def _attach_loop(
    app: FastAPI,
    spec: "agents.definition.AgentSpec",
    runner: object,
    registry: AgentRegistry,
    agent_name: str,
) -> None:
    """Attach a periodic agent loop to the FastAPI app lifespan."""
    from agents.runtime.agent_loop import AgentLoop

    dispatch_endpoint = registry.get_endpoint(spec.lifecycle.dispatch_to)
    dispatch_client = RemoteAgentClient(dispatch_endpoint)
    interval = spec.lifecycle.interval_seconds

    on_success_callback = None
    if spec.lifecycle.on_dispatch_success:
        import importlib

        mod = importlib.import_module(spec.lifecycle.on_dispatch_success.module)
        on_success_callback = getattr(mod, spec.lifecycle.on_dispatch_success.function)

    loop = AgentLoop(
        agent_runner=runner,
        dispatch_client=dispatch_client,
        interval=interval,
        heartbeat_ref={"app": app},
        on_dispatch_success=on_success_callback,
    )

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[None]:
        await loop.start()
        yield
        await loop.stop()

    app.router.lifespan_context = lifespan


# Module-level app creation for uvicorn.
# Only runs when the definition file exists (i.e., inside a container).
# Tests import build_app/load_definition directly.
app: FastAPI | None = None
if Path(AGENT_DEFINITION_PATH).exists():
    try:
        app = build_app()
    except Exception as exc:
        logger.error("Failed to build agent app: %s", exc)
        raise
