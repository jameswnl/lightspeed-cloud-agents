"""Entrypoint for the monitoring agent container.

Creates the FastAPI app with the monitoring agent runner and starts
the monitoring loop as a background task on startup.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from agents.diagnostic.cluster_state import init_scenario
from agents.monitoring.agent import run_monitoring, AGENT_NAME
from agents.monitoring.loop import MonitoringLoop
from agents.remote_agent_client import RemoteAgentClient
from agents.runtime.server import create_app

scenario = os.environ.get("CLUSTER_SCENARIO", "healthy")
init_scenario(scenario)

dispatch_endpoint = os.environ.get("DISPATCH_ENDPOINT", "http://diagnostic-agent:8080")
monitor_interval = int(os.environ.get("MONITOR_INTERVAL", "300"))

dispatch_client = RemoteAgentClient(dispatch_endpoint)

base_app = create_app(agent_runner=run_monitoring, agent_name=AGENT_NAME)

monitoring_loop = MonitoringLoop(
    agent_runner=run_monitoring,
    dispatch_client=dispatch_client,
    interval=monitor_interval,
    heartbeat_ref={"app": base_app},
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start monitoring loop on startup, stop on shutdown."""
    await monitoring_loop.start()
    yield
    await monitoring_loop.stop()


base_app.router.lifespan_context = lifespan
app = base_app
