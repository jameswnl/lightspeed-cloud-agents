"""SubprocessExecutor — spawn: local agent execution in a child process.

Runs an LLM agent with tools in a forked subprocess for process-level
isolation. The child process dies on crash/timeout/memory leak without
affecting the workflow runner.

Uses asyncio.create_subprocess_exec to spawn a child that runs the
agent logic and returns results via stdout (JSON serialized).

No temporalio imports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from cloud_agents.workflow.executor.step.base import StepExecutor, StepInput, StepResult

logger = logging.getLogger(__name__)

_CHILD_MODULE = "cloud_agents.workflow.executor.step.subprocess_child"


def _step_input_to_dict(step_input: StepInput) -> dict[str, Any]:
    """Serialize StepInput to a JSON-safe dict for subprocess transfer.

    Parameters:
        step_input: Step execution input.

    Returns:
        JSON-serializable dict.
    """
    return {
        "prompt": step_input.prompt,
        "system_prompt": step_input.system_prompt,
        "output_schema": step_input.output_schema,
        "tools": step_input.tools,
        "context": step_input.context,
        "provider": step_input.provider,
        "timeout_seconds": step_input.timeout_seconds,
        "sandbox_image": step_input.sandbox_image,
        "workflow_id": step_input.workflow_id,
        "step_name": step_input.step_name,
        "output_key": step_input.output_key,
    }


async def _run_in_subprocess(
    step_input_dict: dict[str, Any],
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run the agent logic in a child process.

    The child process executes subprocess_child module that:
    1. Reads the step input from stdin (JSON)
    2. Calls the LLM with pydantic-ai model_request
    3. Writes the result to stdout (JSON)

    Parameters:
        step_input_dict: Serialized step input.
        timeout_seconds: Maximum execution time before hard kill.

    Returns:
        Result dict with status, output, transcript, tokens.

    Raises:
        asyncio.TimeoutError: If child process exceeds timeout.
        RuntimeError: If child process crashes or returns invalid output.
    """
    env = os.environ.copy()

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        _CHILD_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    input_bytes = json.dumps(step_input_dict).encode()

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() if stderr else "Unknown error"
        raise RuntimeError(f"Child process exited with code {proc.returncode}: {error_msg}")

    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Child process returned invalid JSON: {stdout.decode()[:500]}"
        ) from exc


class SubprocessExecutor(StepExecutor):
    """Execute steps in a forked subprocess for process-level isolation.

    This is the spawn: local implementation. The agent runs in a
    disposable child process — crashes, memory leaks, and timeouts
    are contained. The child process is hard-killed on timeout.
    """

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute a step in a subprocess.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with status, output, transcript, and metrics.
        """
        start_ms = time.monotonic_ns() // 1_000_000

        if step_input.tools:
            logger.warning(
                "SubprocessExecutor does not yet support tools; "
                "tools %s for step '%s' will be ignored. "
                "Use spawn: ephemeral for tool support.",
                step_input.tools,
                step_input.step_name,
            )

        try:
            step_dict = _step_input_to_dict(step_input)

            result = await _run_in_subprocess(
                step_dict,
                timeout_seconds=step_input.timeout_seconds,
            )

            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms

            logger.info(
                "SubprocessExecutor completed step '%s' (%d input, %d output tokens, %dms)",
                step_input.step_name,
                result.get("input_tokens", 0),
                result.get("output_tokens", 0),
                duration_ms,
            )

            return StepResult(
                status=result.get("status", "failed"),
                output=result.get("output"),
                error=result.get("error"),
                transcript=result.get("transcript", []),
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
            error = (
                f"Step '{step_input.step_name}' timed out after "
                f"{step_input.timeout_seconds}s — child process killed"
            )
            logger.error("SubprocessExecutor timeout: %s", error)
            return StepResult(
                status="failed",
                error=error,
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
            logger.error(
                "SubprocessExecutor failed for step '%s': %s",
                step_input.step_name,
                exc,
            )
            return StepResult(
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
            )
