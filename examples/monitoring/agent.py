"""Monitoring agent definition.

Constructs a Pydantic AI Agent that detects cluster anomalies.
Detection only — no remediation tools.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent

from agents.models import AgentRunRequest, AgentRunResponse, MonitoringResult
from examples.agents.monitoring import tools

AGENT_NAME = "monitoring-agent"

INSTRUCTIONS = """\
You are a cluster health monitoring agent. Your job is detection only.

Use get_cluster_summary to check all hosts. Analyze the results and report:
- Any hosts that are not healthy
- Resource usage anomalies (CPU > 80%, disk > 85%)
- Crashed or stopped services

For each issue, create a MonitoringAlert with severity:
- critical: service down, host unreachable, disk > 95%
- high: CPU > 90%, disk > 90%, degraded status
- medium: CPU > 80%, disk > 85%
- low: minor anomalies, informational

Set cluster_healthy=False if any alerts have severity high or critical.
If everything looks good, return an empty alerts list with cluster_healthy=True.
"""


def create_monitoring_agent(model: Any) -> Agent[None, MonitoringResult]:
    """Create the monitoring agent with its read-only tool.

    Args:
        model: The Pydantic AI model to use for inference.

    Returns:
        Configured Agent instance with MonitoringResult output type.
    """
    agent = Agent(
        model,
        output_type=MonitoringResult,
        defer_model_check=True,
        instructions=INSTRUCTIONS,
    )
    agent.tool_plain(tools.get_cluster_summary, docstring_format="google")
    return agent


async def run_monitoring(request: AgentRunRequest) -> AgentRunResponse:
    """Run the monitoring agent and return a structured response.

    Args:
        request: The incoming run request.

    Returns:
        AgentRunResponse with MonitoringResult output.
    """
    import logging

    from examples.agents.monitoring._model import get_model

    logger = logging.getLogger(__name__)
    correlation_id = (request.context or {}).get("correlation_id", "none")
    logger.info(
        "Starting monitoring run",
        extra={"agent_name": AGENT_NAME, "correlation_id": correlation_id},
    )

    try:
        agent = create_monitoring_agent(get_model())
        result = await agent.run(request.prompt)
    except Exception as exc:
        logger.error(
            "Monitoring run failed: %s",
            exc,
            extra={"agent_name": AGENT_NAME, "correlation_id": correlation_id},
        )
        return AgentRunResponse(
            output={},
            output_type="error",
            usage={"input_tokens": 0, "output_tokens": 0},
            agent_name=AGENT_NAME,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    return AgentRunResponse(
        output=result.output.model_dump(),
        output_type="MonitoringResult",
        usage={
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
        agent_name=AGENT_NAME,
        success=True,
    )
