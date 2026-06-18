"""Diagnostic agent definition.

Constructs a Pydantic AI Agent with tools for cluster diagnosis and remediation.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models.openai import OpenAIResponsesModel

from agents.diagnostic import tools
from agents.diagnostic.cluster_state import cluster_state
from agents.models import AgentRunRequest, AgentRunResponse, DiagnosticReport

AGENT_NAME = "diagnostic-agent"

INSTRUCTIONS = """\
You are a cluster diagnostic and remediation agent.

Your workflow:
1. Use list_hosts to discover hosts
2. Use check_host to inspect each host that needs attention
3. Use get_alerts to see active alerts
4. Use get_recent_deploys to check recent deployments
5. For each issue found, use run_remediation to fix it
6. After remediation, use check_host again to verify the fix worked
7. If verification fails, try a different remediation approach

You MUST verify every remediation by checking the host status afterward.
You MUST attempt to fix all issues you find, not just report them.

Return a structured DiagnosticReport when all issues are addressed.
"""


def create_diagnostic_agent(model: Any) -> Agent[None, DiagnosticReport]:
    """Create the diagnostic agent with tools registered.

    Args:
        model: The Pydantic AI model to use for inference.

    Returns:
        Configured Agent instance.
    """
    agent = Agent(
        model,
        output_type=DiagnosticReport,
        retries=3,
        defer_model_check=True,
        instructions=INSTRUCTIONS,
    )

    agent.tool_plain(tools.list_hosts, docstring_format="google")
    agent.tool_plain(tools.check_host, docstring_format="google")
    agent.tool_plain(tools.get_alerts, docstring_format="google")
    agent.tool_plain(tools.get_recent_deploys, docstring_format="google")
    agent.tool_plain(tools.run_remediation, docstring_format="google")

    @agent.output_validator
    async def verify_all_fixed(
        ctx: RunContext[None], report: DiagnosticReport
    ) -> DiagnosticReport:
        """Reject report if hosts are still broken."""
        unhealthy = [
            f"{name} ({h['status']})"
            for name, h in cluster_state["hosts"].items()
            if h["status"] != "healthy"
        ]
        if unhealthy and report.cluster_healthy:
            raise ModelRetry(
                f"Report claims healthy but these hosts are still unhealthy: "
                f"{', '.join(unhealthy)}. Investigate and fix them."
            )
        if unhealthy and not report.actions_taken:
            raise ModelRetry(
                "There are unhealthy hosts but no remediation actions taken. "
                "Use run_remediation tool to fix issues."
            )
        return report

    return agent


async def run_diagnostic(request: AgentRunRequest) -> AgentRunResponse:
    """Run the diagnostic agent and return a structured response.

    This is the agent_runner function passed to create_app().

    Args:
        request: The incoming run request with prompt and optional context.

    Returns:
        AgentRunResponse with DiagnosticReport output on success,
        or an error response on failure.
    """
    import logging

    from agents.diagnostic._model import get_model

    logger = logging.getLogger(__name__)
    correlation_id = (request.context or {}).get("correlation_id", "none")
    logger.info(
        "Starting diagnostic run",
        extra={"agent_name": AGENT_NAME, "correlation_id": correlation_id},
    )

    try:
        agent = create_diagnostic_agent(get_model())
        result = await agent.run(request.prompt)
    except Exception as exc:
        logger.error(
            "Diagnostic run failed: %s",
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
        output_type="DiagnosticReport",
        usage={
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
        agent_name=AGENT_NAME,
        success=True,
    )
