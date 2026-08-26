"""SandboxExecutor — runs steps in OpenShell containers.

Wraps step_runner.run_step() behind the StepExecutor interface.
This is the spawn: ephemeral implementation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from cloud_agents.workflow.executor.step.base import StepExecutor, StepInput, StepResult

logger = logging.getLogger(__name__)


def _sum_result_event_usage(events: list[dict[str, Any]] | None) -> tuple[int, int, float]:
    """Sum token/cost usage from "result"-type transcript events.

    The sandbox agent (lightspeed_agentic.logging.EventLogger) writes one
    "result" event per agent turn with data.input_tokens/output_tokens/
    cost_usd -- these are the real numbers, distinct from the top-level
    StepTranscript.input_tokens/output_tokens/cost_usd fields, which
    step_runner._collect_transcript() never populates.

    Parameters:
        events: Transcript events (dicts with "type" and "data" keys).
            May be None or contain non-dict entries/data if the transcript
            container is malformed or truncated -- both are skipped rather
            than raising.

    Returns:
        (input_tokens, output_tokens, cost_usd) totals across all result
        events -- usually just one, but summed in case a step's agent
        makes multiple turns/calls.
    """
    input_tokens = 0
    output_tokens = 0
    cost_usd = 0.0
    for event in events or []:
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        input_tokens += data.get("input_tokens") or 0
        output_tokens += data.get("output_tokens") or 0
        cost_usd += data.get("cost_usd") or 0.0
    return input_tokens, output_tokens, cost_usd


class SandboxExecutor(StepExecutor):
    """Execute steps in ephemeral sandbox containers via OpenShell.

    Wraps the existing step_runner.run_step() logic behind the
    StepExecutor interface for backward compatibility.
    """

    def __init__(self, spawner: Any = None, transcript_store: Any = None) -> None:
        """Initialize with a spawner and optional transcript store.

        Parameters:
            spawner: AgentSpawner instance for sandbox lifecycle.
            transcript_store: Optional TranscriptStore for persistence.
        """
        self._spawner = spawner
        self._transcript_store = transcript_store

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute step in a sandbox container.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with status, output, transcript, and metrics.

        Raises:
            ValueError: If no spawner is configured.
        """
        if self._spawner is None:
            raise ValueError(
                f"Step '{step_input.step_name}' requires spawn: ephemeral but no "
                "spawner is configured. Either deploy an OpenShell gateway and set "
                "OPENSHELL_GATEWAY_URL, or change the step to spawn: local or spawn: none."
            )

        from cloud_agents.workflow.core.step_runner import run_step

        step_dict = step_input.raw_step or {
            "name": step_input.step_name,
            "prompt": step_input.prompt,
            "output_key": step_input.output_key,
        }

        run_step_input = {
            "step": step_dict,
            "workflow_id": step_input.workflow_id,
            "provider": step_input.provider,
            "sandbox_image": step_input.sandbox_image,
            "skills_image": step_input.skills_image,
            "skills_paths": step_input.skills_paths,
            "mcp_servers": step_input.mcp_servers,
            "context": step_input.context,
        }

        start_ms = time.monotonic_ns() // 1_000_000
        result = await run_step(
            run_step_input,
            spawner=self._spawner,
            transcript_store=self._transcript_store,
            attempt=1,
        )
        duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms

        transcript_data = result.get("transcript", {})
        transcript_events = []
        if isinstance(transcript_data, dict):
            # .get(..., []) only supplies the default when "events" is
            # *absent* -- an explicit `"events": None` (a malformed or
            # truncated transcript container) would otherwise pass None
            # through as StepResult.transcript, which callers expect to
            # always be a list.
            transcript_events = transcript_data.get("events") or []

        input_tokens, output_tokens, cost_usd = _sum_result_event_usage(transcript_events)

        return StepResult(
            status=result.get("status", "failed"),
            output=result.get("output"),
            error=result.get("error"),
            transcript=transcript_events,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )
