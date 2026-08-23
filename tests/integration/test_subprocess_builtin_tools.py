"""Integration test: real subprocess with cloud_agents.tools built-in tools module.

Proves that CLOUD_AGENTS_TOOLS_MODULE=cloud_agents.tools works in a real
subprocess -- the child imports the package and all 3 built-in tools
(kubectl_get, http_request, read_file) are registered in the tool registry.

These tests spawn actual subprocesses. They take ~3-5s each -- that is expected
for real process-boundary tests.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

_BUILTIN_TOOL_NAMES = ["kubectl_get", "http_request", "read_file"]


class TestSubprocessBuiltinTools:
    """Verify built-in tools are available in a real subprocess."""

    @pytest.mark.asyncio
    async def test_child_process_loads_builtin_tools(self) -> None:
        """Real subprocess with tools_module=cloud_agents.tools registers all built-ins.

        The child will fail at the LLM call (no real API key), but it must NOT
        fail at get_tools() with 'Unknown tool'. That would mean the built-in
        tool registry was not reconstructed in the child process.
        """
        input_data = {
            "prompt": "Use kubectl_get to check pods",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": ["kubectl_get"],
            "tools_module": "cloud_agents.tools",
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())

        # Should NOT fail with "Unknown tool" -- built-ins should be registered
        if result["status"] == "failed":
            assert "Unknown tool" not in result.get(
                "error", ""
            ), f"Built-in tool registry not reconstructed in child: {result['error']}"

    @pytest.mark.asyncio
    async def test_all_three_builtins_registered_in_child(self) -> None:
        """All 3 built-in tools (kubectl_get, http_request, read_file) are available in child.

        Sends each tool name individually and verifies none cause an 'Unknown tool'
        error. This proves the cloud_agents.tools __init__.py auto-discovery loop
        works across the process boundary.
        """
        for tool_name in _BUILTIN_TOOL_NAMES:
            input_data = {
                "prompt": f"Use {tool_name}",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "k",
                },
                "tools": [tool_name],
                "tools_module": "cloud_agents.tools",
            }

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "cloud_agents.workflow.executor.step.subprocess_child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=json.dumps(input_data).encode()),
                timeout=15,
            )

            result = json.loads(stdout.decode())

            if result["status"] == "failed":
                assert "Unknown tool" not in result.get("error", ""), (
                    f"Built-in tool '{tool_name}' not found in child process: " f"{result['error']}"
                )

    @pytest.mark.asyncio
    async def test_child_without_tools_module_cannot_find_builtins(self) -> None:
        """Without tools_module, child process cannot find built-in tools.

        This is the negative-path counterpart: without tools_module set, the
        child's tool registry is empty and requesting a built-in tool should
        produce an 'Unknown tool' error. This proves the registration actually
        depends on the tools_module import, not a side effect.
        """
        input_data = {
            "prompt": "Use kubectl_get",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": ["kubectl_get"],
            # No tools_module -- registry will be empty in child
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agents.workflow.executor.step.subprocess_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())

        assert result["status"] == "failed"
        assert "Unknown tool" in result.get("error", ""), (
            "Expected 'Unknown tool' error without tools_module, "
            f"but got: {result.get('error', '')}"
        )
