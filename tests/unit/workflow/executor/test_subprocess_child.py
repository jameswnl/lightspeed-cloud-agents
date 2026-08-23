"""Tests for subprocess_child — child process entry point for SubprocessExecutor."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


class TestSubprocessChildMain:
    """Tests for subprocess_child.main() function."""

    def test_successful_llm_response(self, mocker: MockerFixture) -> None:
        """main() calls LLM and writes successful JSON result to stdout."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = '{"severity": "high"}'
        mock_response.usage = RequestUsage(input_tokens=50, output_tokens=20)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "Classify this alert",
            "system_prompt": "You are a security analyst.",
            "output_schema": {"type": "object"},
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "step_name": "triage",
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        assert result["output"] == {"severity": "high"}
        assert result["input_tokens"] == 50
        assert result["output_tokens"] == 20
        assert "duration_ms" in result

    def test_credentials_resolved(self, mocker: MockerFixture) -> None:
        """main() calls ensure_credentials_env with provider config."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=10, output_tokens=5)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mock_ensure = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        provider = {"name": "openai", "model": "gpt-4o", "credentials_secret": "my-key"}
        input_data = {
            "prompt": "test",
            "provider": provider,
            "context": {},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        mock_ensure.assert_called_once_with(provider)

    def test_llm_error_returns_failed(self, mocker: MockerFixture) -> None:
        """main() returns failed status when LLM call raises."""
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=Exception("Connection refused"),
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "test",
            "provider": {"name": "openai", "model": "gpt-4o"},
            "context": {},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "failed"
        assert "Connection refused" in result["error"]
        assert result["output"] is None
        assert "duration_ms" in result

    def test_non_json_with_output_schema_fails(self, mocker: MockerFixture) -> None:
        """main() returns failed when output_schema set but LLM returns non-JSON."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = "Not valid JSON"
        mock_response.usage = RequestUsage(input_tokens=30, output_tokens=10)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "Classify",
            "provider": {"name": "openai", "model": "gpt-4o"},
            "output_schema": {"type": "object"},
            "context": {},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "failed"
        assert "non-json" in result["error"].lower()

    def test_plain_text_without_schema_wraps(self, mocker: MockerFixture) -> None:
        """main() wraps plain text in response dict when no output_schema."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = "Just a summary."
        mock_response.usage = RequestUsage(input_tokens=20, output_tokens=8)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "Summarize",
            "provider": {"name": "openai", "model": "gpt-4o"},
            "context": {},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        assert result["output"] == {"response": "Just a summary."}

    def test_none_content_without_schema(self, mocker: MockerFixture) -> None:
        """main() handles None content without schema gracefully."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = None
        mock_response.usage = RequestUsage(input_tokens=10, output_tokens=0)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "Summarize",
            "provider": {"name": "openai", "model": "gpt-4o"},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        assert result["output"] == {"response": None}

    def test_context_included_in_user_prompt(self, mocker: MockerFixture) -> None:
        """main() includes prior step context in the user prompt."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = '{"fix": "restart"}'
        mock_response.usage = RequestUsage(input_tokens=30, output_tokens=10)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "Fix the issue",
            "provider": {"name": "openai", "model": "gpt-4o"},
            "context": {"diagnosis": {"output": {"issue": "OOM"}}},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        # Verify the user prompt included context
        call_args = mock_fn.call_args
        request_messages = call_args[0][1]
        user_part = request_messages[0].parts[0]
        assert "OOM" in user_part.content

    def test_model_string_passed_correctly(self, mocker: MockerFixture) -> None:
        """main() passes correct model string to model_request."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=10, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "test",
            "provider": {"name": "anthropic", "model": "claude-sonnet-5"},
            "context": {},
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        mock_fn.assert_called_once()
        assert mock_fn.call_args[0][0] == "anthropic:claude-sonnet-5"


class TestSubprocessChildWithMCPServers:
    """Tests for subprocess_child when MCP servers are configured."""

    def test_mcp_servers_only_dispatches_to_agent(self, mocker: MockerFixture) -> None:
        """main() uses Agent path when mcp_servers present but no tools."""
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_result = mocker.MagicMock()
        mock_result.output = '{"status": "ok"}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.Agent",
        )
        mock_agent_instance = mocker.MagicMock()

        async def fake_run(prompt: str, **kwargs: Any) -> object:
            return mock_result

        mock_agent_instance.run = fake_run
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.StreamableHttpTransport",
        )

        # Ensure model_request is NOT called
        mock_model_req = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
        )

        input_data = {
            "prompt": "Query the cluster",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "mcp_servers": [
                {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
            ],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        mock_agent_cls.assert_called_once()
        mock_model_req.assert_not_called()

    def test_mcp_servers_with_tools(self, mocker: MockerFixture) -> None:
        """main() uses Agent path with both tools and MCP servers."""
        from cloud_agents.workflow.executor.step.tools import clear_tools, register_tool

        clear_tools()
        register_tool("kubectl_get", lambda q: f"result: {q}")

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 60
        mock_usage.output_tokens = 25

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.Agent",
        )
        mock_agent_instance = mocker.MagicMock()

        async def fake_run(prompt: str, **kwargs: Any) -> object:
            return mock_result

        mock_agent_instance.run = fake_run
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.MCPToolset",
            return_value=mock_toolset,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.StreamableHttpTransport",
        )

        input_data = {
            "prompt": "Query the cluster",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "tools": ["kubectl_get"],
            "mcp_servers": [
                {"name": "kubectl", "url": "http://mcp-kubectl:8080/sse"},
            ],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        mock_agent_cls.assert_called_once()

        # Agent should get both tools and toolsets
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "tools" in call_kwargs
        assert "toolsets" in call_kwargs

        clear_tools()

    def test_mcp_servers_with_auth_headers(self, mocker: MockerFixture) -> None:
        """MCP server auth headers are passed to StreamableHttpTransport."""
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.Agent",
        )
        mock_agent_instance = mocker.MagicMock()

        async def fake_run(prompt: str, **kwargs: Any) -> object:
            return mock_result

        mock_agent_instance.run = fake_run
        mock_agent_cls.return_value = mock_agent_instance

        mock_toolset = mocker.MagicMock()
        mock_toolset.__aenter__ = mocker.AsyncMock(return_value=mock_toolset)
        mock_toolset.__aexit__ = mocker.AsyncMock(return_value=False)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.MCPToolset",
            return_value=mock_toolset,
        )
        mock_transport_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.StreamableHttpTransport",
        )

        input_data = {
            "prompt": "Query",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "mcp_servers": [
                {
                    "name": "kubectl",
                    "url": "http://mcp-kubectl:8080/sse",
                    "headers": {"Authorization": "Bearer secret-token"},
                },
            ],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        mock_transport_cls.assert_called_once_with(
            url="http://mcp-kubectl:8080/sse",
            headers={"Authorization": "Bearer secret-token"},
        )


def _dummy_tool(query: str) -> str:
    """A dummy tool for testing.

    Parameters:
        query: Input query.

    Returns:
        Fixed string result.
    """
    return f"result: {query}"


class TestSubprocessChildWithTools:
    """Tests for subprocess_child.main() when tools are present."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> None:
        """Clear tool registry before each test."""
        from cloud_agents.workflow.executor.step.tools import clear_tools

        clear_tools()
        yield  # type: ignore[misc]
        clear_tools()

    def test_uses_agent_when_tools_present(self, mocker: MockerFixture) -> None:
        """main() uses pydantic-ai Agent when tools are provided."""
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 80
        mock_usage.output_tokens = 30

        mock_result = mocker.MagicMock()
        mock_result.output = '{"severity": "high"}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.Agent",
        )
        mock_agent_instance = mocker.MagicMock()

        async def fake_run(prompt: str, **kwargs: Any) -> object:
            return mock_result

        mock_agent_instance.run = fake_run
        mock_agent_cls.return_value = mock_agent_instance

        input_data = {
            "prompt": "Get pods",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "tools": ["kubectl_get"],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        assert result["output"] == {"severity": "high"}
        assert result["input_tokens"] == 80
        assert result["output_tokens"] == 30
        mock_agent_cls.assert_called_once()

    def test_no_tools_uses_model_request(self, mocker: MockerFixture) -> None:
        """main() uses model_request when no tools provided."""
        from pydantic_ai.usage import RequestUsage

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=10, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.Agent",
        )

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "test",
            "provider": {"name": "openai", "model": "gpt-4o"},
            "context": {},
            "tools": [],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "completed"
        mock_fn.assert_called_once()
        mock_agent_cls.assert_not_called()

    def test_unknown_tool_returns_failed(self, mocker: MockerFixture) -> None:
        """main() returns failed when a tool name is unknown."""
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        input_data = {
            "prompt": "test",
            "provider": {"name": "openai", "model": "gpt-4o"},
            "context": {},
            "tools": ["nonexistent_tool"],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "failed"
        assert "Unknown tool" in result["error"]

    def test_agent_with_system_prompt(self, mocker: MockerFixture) -> None:
        """main() passes system_prompt as instructions to Agent."""
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _dummy_tool)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env",
        )

        mock_usage = mocker.MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5

        mock_result = mocker.MagicMock()
        mock_result.output = '{"ok": true}'
        mock_result.usage = mock_usage

        mock_agent_cls = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.Agent",
        )
        mock_agent_instance = mocker.MagicMock()

        async def fake_run(prompt: str, **kwargs: Any) -> object:
            return mock_result

        mock_agent_instance.run = fake_run
        mock_agent_cls.return_value = mock_agent_instance

        input_data = {
            "prompt": "Check pods",
            "system_prompt": "You are a K8s expert.",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "tools": ["kubectl_get"],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert call_kwargs["instructions"] == "You are a K8s expert."
