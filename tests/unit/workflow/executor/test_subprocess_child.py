"""Tests for subprocess_child — child process entry point for SubprocessExecutor."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import AsyncMock

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
