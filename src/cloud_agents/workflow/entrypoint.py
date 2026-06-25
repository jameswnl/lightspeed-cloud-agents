"""Workflow runner entrypoint.

Reads workflow.yaml + registry.yaml, creates WorkflowExecutor,
and serves the workflow HTTP API.

Usage: uvicorn agents.workflow.entrypoint:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI

from agents.registry import AgentRegistry
from agents.runtime.tracing import init_tracing
from agents.workflow.api import create_workflow_app
from agents.workflow.auto_approve import ApprovalPolicy
from agents.workflow.definition import WorkflowDefinition
from agents.workflow.executor import WorkflowExecutor
from agents.workflow.persistence import FilePersistence, InMemoryPersistence, WorkflowPersistence

logger = logging.getLogger(__name__)

WORKFLOW_PATH = os.environ.get("WORKFLOW_DEFINITION", "/app/workflow.yaml")
REGISTRY_PATH = os.environ.get("AGENT_REGISTRY", "/app/registry.yaml")


def _load_workflow(path: str) -> WorkflowDefinition:
    """Load workflow definition from YAML."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"Workflow definition not found: {path}")
    with open(p) as f:
        data = yaml.safe_load(f)
    return WorkflowDefinition.model_validate(data)


def _load_registry(path: str) -> AgentRegistry:
    """Load agent registry from YAML."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(
            f"Agent registry not found: {path}. "
            f"Workflow runner needs a registry to dispatch agent steps."
        )
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    agents = {a["name"]: a["endpoint"] for a in data.get("agents", [])}
    return AgentRegistry(agents)


PERSISTENCE_TYPE = os.environ.get("WORKFLOW_PERSISTENCE", "memory")
PERSISTENCE_PATH = os.environ.get("WORKFLOW_STATE_DIR", "/app/state")
POSTGRES_URL = os.environ.get("WORKFLOW_POSTGRES_URL", "")


CALLBACK_BASE_URL = os.environ.get("CALLBACK_BASE_URL", "")

SPAWNER_TYPE = os.environ.get("WORKFLOW_SPAWNER", "")


def _create_spawner():
    """Create spawner based on environment config."""
    if SPAWNER_TYPE == "kubernetes":
        from agents.spawner.kubernetes_spawner import KubernetesSpawner
        namespace = os.environ.get("SPAWNER_NAMESPACE", "default")
        service_account = os.environ.get("SPAWNER_SERVICE_ACCOUNT", "workflow-runner")
        config_cm = os.environ.get("SPAWNER_CONFIG_CONFIGMAP")
        tools_cm = os.environ.get("SPAWNER_TOOLS_CONFIGMAP")
        logger.info("Using KubernetesSpawner (namespace=%s)", namespace)
        return KubernetesSpawner(
            namespace=namespace, service_account=service_account,
            config_configmap=config_cm, tools_configmap=tools_cm,
        )
    if SPAWNER_TYPE == "podman":
        from agents.spawner.podman_spawner import PodmanSpawner
        network = os.environ.get("SPAWNER_NETWORK", "cloud-agents")
        logger.info("Using PodmanSpawner (network=%s)", network)
        return PodmanSpawner(network=network)
    return None


def _create_persistence() -> WorkflowPersistence:
    """Create persistence backend based on environment config."""
    if PERSISTENCE_TYPE == "postgres" and POSTGRES_URL:
        from agents.workflow.postgres_persistence import PostgresPersistence
        return PostgresPersistence(POSTGRES_URL)
    if PERSISTENCE_TYPE == "file":
        return FilePersistence(PERSISTENCE_PATH)
    return InMemoryPersistence()


def build_workflow_app(
    workflow_path: str = WORKFLOW_PATH,
    registry_path: str = REGISTRY_PATH,
) -> "fastapi.FastAPI":
    """Build the workflow runner FastAPI app."""
    defn = _load_workflow(workflow_path)
    registry = _load_registry(registry_path)
    workflow_name = defn.metadata.get("name", "unknown")
    init_tracing(f"workflow-{workflow_name}")
    persistence = _create_persistence()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Initialize persistence and start recovery poller on startup."""
        if hasattr(persistence, "initialize"):
            logger.info("Initializing persistence backend: %s", type(persistence).__name__)
            await persistence.initialize()

        from agents.workflow.advancement import RecoveryPoller
        poller = RecoveryPoller(persistence)
        poller_task = asyncio.create_task(poller.start())
        yield
        await poller.stop()
        poller_task.cancel()

    spawner = _create_spawner()
    agent_image = os.environ.get("AGENT_IMAGE", "agent-runtime:latest")

    executor = WorkflowExecutor(
            defn, registry,
            persistence=persistence,
            approval_policy=ApprovalPolicy(),
            spawner=spawner,
            agent_image=agent_image,
            callback_base_url=CALLBACK_BASE_URL,
        )
    return create_workflow_app(executor, workflow_name, lifespan=_lifespan)


def build_stateless_app() -> "fastapi.FastAPI":
    """Build a stateless workflow runner that starts with an empty catalog.

    Definitions are submitted via the API. No workflow.yaml required.
    """
    registry = _load_registry(REGISTRY_PATH) if Path(REGISTRY_PATH).exists() else AgentRegistry({})
    init_tracing("workflow-runner")
    persistence = _create_persistence()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        if hasattr(persistence, "initialize"):
            await persistence.initialize()
        from agents.workflow.advancement import RecoveryPoller
        poller = RecoveryPoller(persistence)
        poller_task = asyncio.create_task(poller.start())
        yield
        await poller.stop()
        poller_task.cancel()

    from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
    placeholder_defn = WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "placeholder"},
        spec=WorkflowSpec(steps=[
            WorkflowStepSpec(name="noop", type="agent", agent="none",
                             prompt="", output_key="noop", spawn="pre-deployed"),
        ]),
    )

    spawner = _create_spawner()
    agent_image = os.environ.get("AGENT_IMAGE", "agent-runtime:latest")

    executor = WorkflowExecutor(
        placeholder_defn, registry,
        persistence=persistence,
        approval_policy=ApprovalPolicy(),
        spawner=spawner,
        agent_image=agent_image,
        callback_base_url=CALLBACK_BASE_URL,
    )
    return create_workflow_app(executor, "workflow-runner", lifespan=_lifespan)


app = None
if Path(WORKFLOW_PATH).exists():
    try:
        app = build_workflow_app()
    except Exception as exc:
        logger.error("Failed to build workflow app: %s", exc)
        raise
else:
    try:
        app = build_stateless_app()
        logger.info("Started stateless workflow runner (no workflow.yaml)")
    except Exception as exc:
        logger.error("Failed to build stateless workflow app: %s", exc)
        raise
