"""Workflow runner entrypoint.

Reads workflow.yaml + registry.yaml, creates WorkflowExecutor,
and serves the workflow HTTP API.

Usage: uvicorn agents.workflow.entrypoint:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from agents.registry import AgentRegistry
from agents.workflow.api import create_workflow_app
from agents.workflow.definition import WorkflowDefinition
from agents.workflow.executor import WorkflowExecutor

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


def build_workflow_app(
    workflow_path: str = WORKFLOW_PATH,
    registry_path: str = REGISTRY_PATH,
) -> "fastapi.FastAPI":
    """Build the workflow runner FastAPI app."""
    defn = _load_workflow(workflow_path)
    registry = _load_registry(registry_path)
    workflow_name = defn.metadata.get("name", "unknown")

    executor = WorkflowExecutor(defn, registry)
    return create_workflow_app(executor, workflow_name)


app = None
if Path(WORKFLOW_PATH).exists():
    try:
        app = build_workflow_app()
    except Exception as exc:
        logger.error("Failed to build workflow app: %s", exc)
        raise
