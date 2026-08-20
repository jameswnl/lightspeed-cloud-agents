"""SubprocessExecutor — spawn: local LLM execution in a child process.

Executes a single LLM call in a forked subprocess for process-level
isolation. The child process dies on crash/timeout/memory leak without
affecting the workflow runner. Reuses DirectExecutor's _call_llm and
_build_messages to avoid logic duplication.

Uses asyncio.create_subprocess_exec to spawn a child that imports and
runs the LLM call logic, returning results via stdout (JSON serialized).

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

    The child process executes a self-contained Python script that:
    1. Reads the step input from stdin (JSON)
    2. Calls the LLM with the prompt (using httpx, same as DirectExecutor)
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
    child_script = _CHILD_PROCESS_SCRIPT

    env = os.environ.copy()

    provider = step_input_dict.get("provider", {})
    cred_secret = provider.get("credentials_secret", "")
    if cred_secret:
        env_key = cred_secret.upper().replace("-", "_")
        api_key = os.environ.get(env_key) or os.environ.get(cred_secret)
        if api_key:
            env[env_key] = api_key

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", child_script,
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


_CHILD_PROCESS_SCRIPT = '''
import asyncio
import json
import sys

def main():
    input_data = json.loads(sys.stdin.read())
    provider = input_data.get("provider", {})
    step_name = input_data.get("step_name", "unknown")
    timeout_seconds = input_data.get("timeout_seconds", 600)

    from cloud_agents.workflow.executor.step.base import StepInput
    from cloud_agents.workflow.executor.step.direct import (
        _build_messages, _call_llm, _parse_output,
    )

    step_input = StepInput(
        prompt=input_data["prompt"],
        system_prompt=input_data.get("system_prompt"),
        output_schema=input_data.get("output_schema"),
        tools=input_data.get("tools", []),
        context=input_data.get("context", {}),
        provider=provider,
        timeout_seconds=timeout_seconds,
        workflow_id=input_data.get("workflow_id", ""),
        step_name=step_name,
        output_key=input_data.get("output_key", ""),
    )

    try:
        messages = _build_messages(step_input)
        llm_result = asyncio.run(_call_llm(
            provider=provider,
            messages=messages,
            output_schema=step_input.output_schema,
            timeout_seconds=timeout_seconds,
        ))

        content = llm_result["content"]
        output = _parse_output(content)

        result = {
            "status": "completed",
            "output": output,
            "transcript": [{"type": "llm.call", "model": provider.get("model", "unknown"), "step_name": step_name}],
            "input_tokens": llm_result.get("input_tokens", 0),
            "output_tokens": llm_result.get("output_tokens", 0),
        }
    except Exception as e:
        result = {"status": "failed", "output": None, "error": str(e),
                  "transcript": [], "input_tokens": 0, "output_tokens": 0}

    print(json.dumps(result))

main()
'''


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
