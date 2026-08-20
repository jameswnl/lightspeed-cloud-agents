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
from typing import Any, Optional

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


def _resolve_api_key(provider: dict[str, Any]) -> str | None:
    """Resolve LLM API key from provider config and environment.

    Parameters:
        provider: Provider configuration dict.

    Returns:
        API key string or None if not found.
    """
    cred_secret = provider.get("credentials_secret", "")
    if cred_secret:
        env_key = cred_secret.upper().replace("-", "_")
        return os.environ.get(env_key) or os.environ.get(cred_secret)
    return None


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
import json
import sys
import os

def main():
    input_data = json.loads(sys.stdin.read())
    prompt = input_data["prompt"]
    system_prompt = input_data.get("system_prompt")
    output_schema = input_data.get("output_schema")
    provider = input_data.get("provider", {})
    context = input_data.get("context", {})
    step_name = input_data.get("step_name", "unknown")

    cred_secret = provider.get("credentials_secret", "")
    env_key = cred_secret.upper().replace("-", "_") if cred_secret else ""
    api_key = os.environ.get(env_key) or os.environ.get(cred_secret) if cred_secret else None

    if not api_key:
        provider_name = provider.get("name", "openai")
        default_keys = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
        default_key = default_keys.get(provider_name, "")
        api_key = os.environ.get(default_key) if default_key else None

    if not api_key:
        result = {"status": "failed", "output": None, "error": "API key not found",
                  "transcript": [], "input_tokens": 0, "output_tokens": 0}
        print(json.dumps(result))
        return

    import httpx

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    user_content = prompt
    if context:
        context_parts = []
        for key, value in context.items():
            if isinstance(value, dict) and value.get("output"):
                context_parts.append(f"Previous step \\'{key}\\': {json.dumps(value['output'])}")
        if context_parts:
            user_content = user_content + "\\n\\n--- Prior step outputs ---\\n" + "\\n\\n".join(context_parts)

    if output_schema:
        user_content += "\\n\\nRespond with JSON matching this schema:\\n" + json.dumps(output_schema, indent=2)

    messages.append({"role": "user", "content": user_content})

    base_urls = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1"}
    base_url = provider.get("base_url", base_urls.get(provider.get("name", "openai"), base_urls["openai"]))
    model = provider.get("model", "gpt-4o")

    request_body = {"model": model, "messages": messages}
    if output_schema:
        request_body["response_format"] = {"type": "json_object"}

    try:
        with httpx.Client(timeout=300) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=request_body,
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        try:
            output = json.loads(content)
        except json.JSONDecodeError:
            output = {"response": content}

        result = {
            "status": "completed",
            "output": output,
            "transcript": [{"type": "llm.call", "model": model, "step_name": step_name}],
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
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
