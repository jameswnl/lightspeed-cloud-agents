"""Entrypoint for the diagnostic agent container.

Creates the FastAPI app with the diagnostic agent runner.
Used by uvicorn: uvicorn agents.diagnostic.entrypoint:app
"""

from examples.agents.diagnostic.agent import run_diagnostic, AGENT_NAME
from examples.agents.diagnostic.cluster_state import reset_cluster_healthy
from agents.runtime.server import create_app

reset_cluster_healthy()

app = create_app(agent_runner=run_diagnostic, agent_name=AGENT_NAME)
