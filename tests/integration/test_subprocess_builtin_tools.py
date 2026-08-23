"""Integration: subprocess child with built-in tools module."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest


class TestSubprocessBuiltinTools:
    """Verify subprocess child loads cloud_agents.tools built-in module."""

    @pytest.mark.asyncio
    async def test_child_loads_builtin_tools(self) -> None:
        """Real subprocess with tools_module=cloud_agents.tools registers built-ins."""
        input_data = {
            "prompt": "test",
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

        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())

        # Child will fail at LLM call (no real API key), but should NOT
        # fail at get_tools() — the tools_module import should have
        # populated the registry with built-in tools.
        if result["status"] == "failed":
            assert "Unknown tool" not in result.get("error", ""), (
                f"Bootstrap failed: {result['error']}"
            )

    @pytest.mark.asyncio
    async def test_all_three_builtins(self) -> None:
        """All 3 built-in tools available in child process."""
        for tool in ["kubectl_get", "http_request", "read_file"]:
            input_data = {
                "prompt": "test",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "k",
                },
                "tools": [tool],
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
                proc.communicate(json.dumps(input_data).encode()),
                timeout=15,
            )

            result = json.loads(stdout.decode())
            if result["status"] == "failed":
                assert "Unknown tool" not in result.get("error", ""), (
                    f"Built-in '{tool}' not found"
                )

    @pytest.mark.asyncio
    async def test_without_tools_module_fails(self) -> None:
        """Without tools_module, built-ins are not found."""
        input_data = {
            "prompt": "test",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "k",
            },
            "tools": ["kubectl_get"],
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
            proc.communicate(json.dumps(input_data).encode()),
            timeout=15,
        )

        result = json.loads(stdout.decode())
        assert result["status"] == "failed"
        assert "Unknown tool" in result.get("error", "")
