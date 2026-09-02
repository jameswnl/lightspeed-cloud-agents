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


class TestSubprocessChildWithSkills:
    """Tests for subprocess_child skills capability integration."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> None:
        """Clear tool registry before each test."""
        from cloud_agents.workflow.executor.step.tools import clear_tools

        clear_tools()
        yield  # type: ignore[misc]
        clear_tools()

    def test_child_agent_receives_capabilities_when_env_set(self, mocker: MockerFixture) -> None:
        """Child Agent gets capabilities when CLOUD_AGENTS_SKILLS_PATHS is set."""
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

        mock_cap = mocker.MagicMock()
        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.get_skills_capability",
            return_value=mock_cap,
        )

        input_data = {
            "prompt": "Check pods",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "tools": ["kubectl_get"],
            "allowed_skills": ["k8s-diag"],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "capabilities" in call_kwargs
        assert call_kwargs["capabilities"] == [mock_cap]

    def test_child_agent_no_capabilities_when_env_unset(self, mocker: MockerFixture) -> None:
        """Child Agent has no capabilities when CLOUD_AGENTS_SKILLS_PATHS is unset."""
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

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.get_skills_capability",
            return_value=None,
        )

        input_data = {
            "prompt": "Check pods",
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "context": {},
            "tools": ["kubectl_get"],
            "allowed_skills": ["k8s-diag"],
        }

        stdin_mock = StringIO(json.dumps(input_data))
        stdout_mock = StringIO()

        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        from cloud_agents.workflow.executor.step.subprocess_child import main

        main()

        call_kwargs = mock_agent_cls.call_args.kwargs
        assert call_kwargs.get("capabilities") is None


class TestParseContent:
    """Tests for _parse_content's markdown-fence stripping (parity with direct.py#_parse_output).

    Regression coverage for issue #227: this parity gap was never
    fixed for spawn: local's subprocess_child.py after #188 fixed it
    for spawn: none's direct.py, so any spawn: local step with
    output_schema failed whenever the model fenced its JSON response
    (which gpt-4o-mini does reliably) -- surfaced by a real-LLM e2e
    test driving SubprocessExecutor through LocalWorkflowRunner.
    """

    def test_json_fence_stripped_with_schema(self) -> None:
        """```json ... ``` fenced JSON is parsed when output_schema is set."""
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        content = '```json\n{"severity": "high", "reason": "cpu spike"}\n```'
        result = _parse_content(content, {"type": "object"})
        assert result == {
            "status": "completed",
            "output": {"severity": "high", "reason": "cpu spike"},
        }

    def test_uppercase_json_tag_fence_stripped(self) -> None:
        """```JSON ... ``` (uppercase tag) is also stripped, not just lowercase.

        Parity with test_direct_executor.py's equivalent case -- the
        regex already has re.IGNORECASE, this just closes the test
        coverage loop to match.
        """
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        content = '```JSON\n{"ok": true}\n```'
        result = _parse_content(content, {"type": "object"})
        assert result == {"status": "completed", "output": {"ok": True}}

    def test_bare_fence_without_json_tag_stripped(self) -> None:
        """``` ... ``` (no "json" tag) is also stripped."""
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        content = '```\n{"ok": true}\n```'
        result = _parse_content(content, {"type": "object"})
        assert result == {"status": "completed", "output": {"ok": True}}

    def test_fence_stripped_without_schema_too(self) -> None:
        """Fence-stripping also applies on the no-schema path."""
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        content = '```json\n{"ok": true}\n```'
        result = _parse_content(content, None)
        assert result == {"status": "completed", "output": {"ok": True}}

    def test_non_fenced_json_unaffected(self) -> None:
        """Plain (non-fenced) JSON still parses correctly."""
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        result = _parse_content('{"severity": "low"}', {"type": "object"})
        assert result == {"status": "completed", "output": {"severity": "low"}}

    def test_still_fails_for_genuinely_non_json_fenced_content(self) -> None:
        """A fence wrapping non-JSON text still fails -- stripping isn't a cure-all."""
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        result = _parse_content("```\nnot actually json\n```", {"type": "object"})
        assert result["status"] == "failed"
        assert "non-json" in result["error"].lower()

    def test_falls_back_to_original_content_on_unfenced_parse_failure(self) -> None:
        """No-schema path preserves the original (unstripped) text on parse failure."""
        from cloud_agents.workflow.executor.step.subprocess_child import _parse_content

        content = "```\nnot json at all\n```"
        result = _parse_content(content, None)
        assert result == {"status": "completed", "output": {"response": content}}


class TestRunModelRequestNativeStructuredOutput:
    """Tests for _run_model_request's native structured-output attempt + fallback (#235).

    Mirrors test_direct_executor.py's TestCallLlmNativeStructuredOutput --
    subprocess_child.py's spawn: local model_request path previously had no
    equivalent to direct.py's _call_llm native-mode attempt, so a mock/test
    LLM that only honors a native json_schema response format (as opposed to
    a plain-text schema hint) would satisfy spawn: none but not spawn: local
    for the identical request.
    """

    def _base_input(self, output_schema: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "prompt": "hi",
            "provider": {"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "k"},
            "output_schema": output_schema,
            "context": {},
        }

    @pytest.mark.asyncio
    async def test_output_schema_triggers_native_mode_request(self, mocker: MockerFixture) -> None:
        """When output_schema is object-rooted, model_request is called with native output_mode."""
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )

        await _run_model_request(self._base_input({"type": "object"}))

        mock_fn.assert_called_once()
        params = mock_fn.call_args.kwargs["model_request_parameters"]
        assert params.output_mode == "native"
        assert params.output_object.json_schema == {"type": "object"}

    @pytest.mark.asyncio
    async def test_no_output_schema_skips_native_mode(self, mocker: MockerFixture) -> None:
        """Without output_schema, model_request is called without model_request_parameters."""
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = "plain text"
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )

        await _run_model_request(self._base_input(None))

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("model_request_parameters") is None

    @pytest.mark.asyncio
    async def test_falls_back_when_native_mode_raises_user_error(
        self, mocker: MockerFixture
    ) -> None:
        """If native mode isn't supported (UserError), retries without it."""
        from pydantic_ai.exceptions import UserError
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=[UserError("native mode not supported"), mock_response],
        )

        result = await _run_model_request(self._base_input({"type": "object"}))

        assert mock_fn.call_count == 2
        assert mock_fn.call_args_list[0].kwargs.get("model_request_parameters") is not None
        assert mock_fn.call_args_list[1].kwargs.get("model_request_parameters") is None
        assert result["output"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_non_object_root_schema_skips_native_mode(self, mocker: MockerFixture) -> None:
        """A non-object-root output_schema (e.g. top-level array) skips native mode.

        Mirrors direct.py's _supports_native_output guard: OpenAI's
        Structured Outputs requires an object-rooted JSON Schema, and
        output_schema is user-authored workflow YAML with no such guarantee.
        """
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = '["a", "b"]'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        )

        result = await _run_model_request(
            self._base_input({"type": "array", "items": {"type": "string"}})
        )

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("model_request_parameters") is None
        assert result["output"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_non_user_error_propagates_without_fallback(self, mocker: MockerFixture) -> None:
        """A non-UserError exception (e.g. a real API failure) propagates -- no silent retry."""
        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network exploded"),
        )

        with pytest.raises(RuntimeError, match="network exploded"):
            await _run_model_request(self._base_input({"type": "object"}))

        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_falls_back_on_400_model_http_error(self, mocker: MockerFixture) -> None:
        """A 400 ModelHTTPError (provider rejected the native schema) triggers fallback."""
        from pydantic_ai.exceptions import ModelHTTPError
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=[
                ModelHTTPError(status_code=400, model_name="gpt-4o-mini", body="bad schema"),
                mock_response,
            ],
        )

        result = await _run_model_request(self._base_input({"type": "object"}))

        assert mock_fn.call_count == 2
        assert mock_fn.call_args_list[0].kwargs.get("model_request_parameters") is not None
        assert mock_fn.call_args_list[1].kwargs.get("model_request_parameters") is None
        assert result["output"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_5xx_model_http_error_propagates_without_fallback(
        self, mocker: MockerFixture
    ) -> None:
        """A 5xx ModelHTTPError (provider/infra failure, not a schema issue) propagates."""
        from pydantic_ai.exceptions import ModelHTTPError

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_fn = mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=ModelHTTPError(status_code=503, model_name="gpt-4o-mini", body="down"),
        )

        with pytest.raises(ModelHTTPError):
            await _run_model_request(self._base_input({"type": "object"}))

        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_user_error_fallback_logs_warning(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """UserError fallback is logged, so a silent native-mode degradation is
        visible in SubprocessExecutor's surfaced stderr (#235 follow-up)."""
        from pydantic_ai.exceptions import UserError
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=[UserError("native mode not supported"), mock_response],
        )

        with caplog.at_level(
            "WARNING", logger="cloud_agents.workflow.executor.step.subprocess_child"
        ):
            await _run_model_request(self._base_input({"type": "object"}))

        assert any("falling back" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_400_fallback_logs_warning(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 400 ModelHTTPError fallback is also logged."""
        from pydantic_ai.exceptions import ModelHTTPError
        from pydantic_ai.usage import RequestUsage

        from cloud_agents.workflow.executor.step.subprocess_child import _run_model_request

        mock_response = mocker.MagicMock()
        mock_response.text = '{"ok": true}'
        mock_response.usage = RequestUsage(input_tokens=5, output_tokens=5)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child.model_request",
            new_callable=AsyncMock,
            side_effect=[
                ModelHTTPError(status_code=400, model_name="gpt-4o-mini", body="bad schema"),
                mock_response,
            ],
        )

        with caplog.at_level(
            "WARNING", logger="cloud_agents.workflow.executor.step.subprocess_child"
        ):
            await _run_model_request(self._base_input({"type": "object"}))

        assert any("falling back" in record.message for record in caplog.records)
