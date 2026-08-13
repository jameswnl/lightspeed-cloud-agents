"""Unified entrypoint that selects the right executor based on WORKFLOW_ENGINE.

Usage: uvicorn cloud_agents.entrypoint:app --host 0.0.0.0 --port 8080

WORKFLOW_ENGINE=local    → local executor (pydantic-graph, no Temporal) [default]
WORKFLOW_ENGINE=temporal → Temporal executor
"""

import os

WORKFLOW_ENGINE = os.environ.get("WORKFLOW_ENGINE", "local")

if WORKFLOW_ENGINE == "local":
    from cloud_agents.workflow.executor.local.entrypoint import app  # noqa: F401
else:
    from cloud_agents.workflow.executor.temporal.entrypoint import app  # noqa: F401
