"""Tests for SubprocessExecutor — spawn: local pydantic-ai agent in subprocess."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from pytest_mock import MockerFixture


class TestSubprocessExecutorInstantiation:
    """Tests for SubprocessExecutor creation."""

    def test_implements_step_executor(self) -> None:
        """SubprocessExecutor is a StepExecutor."""
        from cloud_agents.workflow.executor.step.base import StepExecutor
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        assert issubclass(SubprocessExecutor, StepExecutor)

    def test_construction(self) -> None:
        """SubprocessExecutor can be instantiated with no arguments."""
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        executor = SubprocessExecutor()
        assert executor is not None

    def test_no_temporal_imports(self) -> None:
        """SubprocessExecutor has zero temporalio imports."""
        from cloud_agents.workflow.executor.step import subprocess_exec as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source


class TestSubprocessExecutorRun:
    """Tests for SubprocessExecutor.run() — subprocess execution."""

    @pytest.mark.asyncio
    async def test_runs_and_returns_result(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor runs agent in subprocess and returns result."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mock_result = {
            "status": "completed",
            "output": {"finding": "disk full on /var"},
            "transcript": [{"type": "llm.call", "model": "gpt-4o"}],
            "input_tokens": 200,
            "output_tokens": 100,
        }

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            return_value=mock_result,
        )

        executor = SubprocessExecutor()
        result = await executor.run(
            StepInput(
                prompt="Investigate the disk usage",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "openai-api-key",
                },
                tools=["kubectl_get", "read_logs"],
                workflow_id="wf-1",
                step_name="investigate",
                output_key="investigation",
            )
        )

        assert result.status == "completed"
        assert result.output == {"finding": "disk full on /var"}
        assert result.input_tokens == 200
        assert result.output_tokens == 100

    @pytest.mark.asyncio
    async def test_subprocess_failure_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor returns failed when subprocess errors."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=RuntimeError("Child process crashed"),
        )

        executor = SubprocessExecutor()
        result = await executor.run(
            StepInput(
                prompt="Investigate",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert result.status == "failed"
        assert "crashed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor returns failed on timeout."""
        import asyncio

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=asyncio.TimeoutError(),
        )

        executor = SubprocessExecutor()
        result = await executor.run(
            StepInput(
                prompt="Slow task",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                timeout_seconds=5,
            )
        )

        assert result.status == "failed"
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor fails when credentials are missing."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        env_copy = {
            k: v for k, v in os.environ.items() if "OPENAI" not in k and "ANTHROPIC" not in k
        }
        mocker.patch.dict(os.environ, env_copy, clear=True)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=ValueError("API key not found"),
        )

        executor = SubprocessExecutor()
        result = await executor.run(
            StepInput(
                prompt="test",
                provider={
                    "name": "openai",
                    "model": "gpt-4o",
                    "credentials_secret": "openai-api-key",
                },
            )
        )

        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_duration_tracked(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor tracks execution duration."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            return_value={
                "status": "completed",
                "output": {"ok": True},
                "transcript": [],
                "input_tokens": 10,
                "output_tokens": 5,
            },
        )

        executor = SubprocessExecutor()
        result = await executor.run(
            StepInput(
                prompt="Quick check",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_context_passed_to_subprocess(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor passes prior step context to the subprocess."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        captured_input: dict[str, Any] = {}

        async def mock_subprocess(step_input_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            captured_input.update(step_input_dict)
            return {
                "status": "completed",
                "output": {"ok": True},
                "transcript": [],
                "input_tokens": 10,
                "output_tokens": 5,
            }

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=mock_subprocess,
        )

        executor = SubprocessExecutor()
        await executor.run(
            StepInput(
                prompt="Fix based on diagnosis",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                context={
                    "diagnosis": {
                        "status": "completed",
                        "output": {"issue": "OOM"},
                    },
                },
            )
        )

        assert "context" in captured_input
        assert captured_input["context"]["diagnosis"]["output"]["issue"] == "OOM"


class TestSubprocessExecutorDispatch:
    """Tests for step dispatch integration."""

    def test_dispatch_returns_subprocess_executor(self) -> None:
        """get_step_executor returns SubprocessExecutor for spawn: local."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        step = {"name": "investigate", "type": "agent", "spawn": "local"}
        executor = get_step_executor(step, spawner=None)
        assert isinstance(executor, SubprocessExecutor)

    def test_dispatch_no_spawner_needed(self) -> None:
        """spawn: local works without any spawner configured."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        step = {"name": "investigate", "type": "agent", "spawn": "local"}
        executor = get_step_executor(step, spawner=None)
        assert executor is not None


class TestChildProcessPayload:
    """Tests for the child process payload serialization."""

    def test_step_input_to_dict(self) -> None:
        """StepInput is serializable to dict for subprocess transfer."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import _step_input_to_dict

        step_input = StepInput(
            prompt="Investigate",
            system_prompt="You are a K8s expert",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            tools=["kubectl_get"],
            context={"prior": {"status": "completed", "output": {"ok": True}}},
            timeout_seconds=120,
            workflow_id="wf-1",
            step_name="investigate",
            output_key="result",
        )

        payload = _step_input_to_dict(step_input)
        assert payload["prompt"] == "Investigate"
        assert payload["system_prompt"] == "You are a K8s expert"
        assert payload["tools"] == ["kubectl_get"]
        assert payload["timeout_seconds"] == 120

        roundtrip = json.dumps(payload)
        assert isinstance(roundtrip, str)


class TestChildProcessPayloadMCPServers:
    """Tests for mcp_servers serialization in child process payload."""

    def test_step_input_to_dict_includes_mcp_servers(self) -> None:
        """_step_input_to_dict serializes mcp_servers for subprocess transfer."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import _step_input_to_dict

        step_input = StepInput(
            prompt="Query the cluster",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            mcp_servers=[
                {
                    "name": "kubectl",
                    "url": "http://mcp-kubectl:8080/sse",
                    "headers": {"Authorization": "Bearer token"},
                },
            ],
        )

        payload = _step_input_to_dict(step_input)
        assert "mcp_servers" in payload
        assert len(payload["mcp_servers"]) == 1
        assert payload["mcp_servers"][0]["name"] == "kubectl"
        assert payload["mcp_servers"][0]["url"] == "http://mcp-kubectl:8080/sse"
        assert payload["mcp_servers"][0]["headers"]["Authorization"] == "Bearer token"

        # Verify round-trip JSON serialization works
        roundtrip = json.dumps(payload)
        assert isinstance(roundtrip, str)

    def test_step_input_to_dict_none_mcp_servers(self) -> None:
        """_step_input_to_dict serializes None mcp_servers."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import _step_input_to_dict

        step_input = StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o"},
        )

        payload = _step_input_to_dict(step_input)
        assert "mcp_servers" in payload
        assert payload["mcp_servers"] is None


class TestSubprocessCancellation:
    """Tests for cancellation handling."""

    @pytest.mark.asyncio
    async def test_cancellation_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor propagates CancelledError after cleanup."""
        import asyncio

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=asyncio.CancelledError(),
        )

        executor = SubprocessExecutor()
        with pytest.raises(asyncio.CancelledError):
            await executor.run(
                StepInput(
                    prompt="Long task",
                    provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
                )
            )


class TestSubprocessTraceparentPropagation:
    """Tests for TRACEPARENT env var injection into child process."""

    @pytest.mark.asyncio
    async def test_traceparent_injected_when_tracing_active(
        self, mocker: MockerFixture
    ) -> None:
        """TRACEPARENT env var is set when OTEL tracing produces a traceparent."""
        captured_env: dict[str, Any] = {}

        async def mock_create_subprocess(*args: Any, **kwargs: Any) -> Any:
            captured_env.update(kwargs.get("env", {}))
            proc = mocker.AsyncMock()
            proc.communicate = mocker.AsyncMock(
                return_value=(
                    json.dumps({
                        "status": "completed",
                        "output": {"ok": True},
                        "input_tokens": 10,
                        "output_tokens": 5,
                    }).encode(),
                    b"",
                )
            )
            proc.returncode = 0
            return proc

        mocker.patch(
            "asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess,
        )

        # Mock inject_traceparent to simulate active tracing
        def mock_inject(headers: dict[str, str]) -> dict[str, str]:
            headers["traceparent"] = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
            return headers

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec.inject_traceparent",
            side_effect=mock_inject,
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        executor = SubprocessExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert "TRACEPARENT" in captured_env
        assert captured_env["TRACEPARENT"] == "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    @pytest.mark.asyncio
    async def test_no_traceparent_when_tracing_inactive(
        self, mocker: MockerFixture
    ) -> None:
        """TRACEPARENT env var is not set when OTEL tracing is not active."""
        captured_env: dict[str, Any] = {}

        async def mock_create_subprocess(*args: Any, **kwargs: Any) -> Any:
            captured_env.update(kwargs.get("env", {}))
            proc = mocker.AsyncMock()
            proc.communicate = mocker.AsyncMock(
                return_value=(
                    json.dumps({
                        "status": "completed",
                        "output": {"ok": True},
                        "input_tokens": 10,
                        "output_tokens": 5,
                    }).encode(),
                    b"",
                )
            )
            proc.returncode = 0
            return proc

        mocker.patch(
            "asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess,
        )

        # Mock inject_traceparent to simulate NO active tracing (NoOp)
        def mock_inject(headers: dict[str, str]) -> dict[str, str]:
            return headers  # No traceparent injected

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec.inject_traceparent",
            side_effect=mock_inject,
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        executor = SubprocessExecutor()
        await executor.run(
            StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            )
        )

        assert "TRACEPARENT" not in captured_env


class TestSubprocessChildStderrSurfacing:
    """Tests for surfacing child stderr on a successful run (#235 follow-up).

    subprocess_child.py logs a warning to stderr when native structured
    output falls back to the prompt-text schema hint (see
    test_subprocess_child.py's TestRunModelRequestNativeStructuredOutput
    fallback-logging tests). Previously stderr was only read and used to
    build an error message when the child exited non-zero -- on success
    (returncode 0) it was silently discarded, so that warning never
    reached the workflow runner's logs. This surfaces non-empty stderr
    from a successful child run via the parent's own logger.
    """

    @pytest.mark.asyncio
    async def test_stderr_logged_on_successful_run(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-empty stderr from a returncode-0 child is logged as a warning."""
        from cloud_agents.workflow.executor.step.subprocess_exec import _run_in_subprocess

        async def mock_create_subprocess(*args: Any, **kwargs: Any) -> Any:
            proc = mocker.AsyncMock()
            proc.communicate = mocker.AsyncMock(
                return_value=(
                    json.dumps(
                        {"status": "completed", "output": {"ok": True}, "input_tokens": 1, "output_tokens": 1}
                    ).encode(),
                    b"WARNING:cloud_agents.workflow.executor.step.subprocess_child:"
                    b"Native structured output not supported, falling back\n",
                )
            )
            proc.returncode = 0
            return proc

        mocker.patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess)

        with caplog.at_level(
            "WARNING", logger="cloud_agents.workflow.executor.step.subprocess_exec"
        ):
            result = await _run_in_subprocess({"prompt": "hi", "step_name": "triage"})

        assert result["status"] == "completed"
        assert any("falling back" in record.message for record in caplog.records)
        assert any("triage" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_no_log_when_stderr_empty(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty stderr on a successful run produces no warning log."""
        from cloud_agents.workflow.executor.step.subprocess_exec import _run_in_subprocess

        async def mock_create_subprocess(*args: Any, **kwargs: Any) -> Any:
            proc = mocker.AsyncMock()
            proc.communicate = mocker.AsyncMock(
                return_value=(
                    json.dumps(
                        {"status": "completed", "output": {"ok": True}, "input_tokens": 1, "output_tokens": 1}
                    ).encode(),
                    b"",
                )
            )
            proc.returncode = 0
            return proc

        mocker.patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess)

        with caplog.at_level(
            "WARNING", logger="cloud_agents.workflow.executor.step.subprocess_exec"
        ):
            await _run_in_subprocess({"prompt": "hi", "step_name": "triage"})

        assert caplog.records == []


class TestSubprocessChildModuleInvocation:
    """Tests verifying subprocess uses module invocation."""

    def test_child_module_constant(self) -> None:
        """SubprocessExecutor uses module invocation, not inline script."""
        from cloud_agents.workflow.executor.step import subprocess_exec as mod

        assert hasattr(mod, "_CHILD_MODULE")
        assert mod._CHILD_MODULE == "cloud_agents.workflow.executor.step.subprocess_child"

    def test_no_inline_child_script(self) -> None:
        """SubprocessExecutor no longer contains inline child script."""
        from cloud_agents.workflow.executor.step import subprocess_exec as mod

        assert not hasattr(mod, "_CHILD_PROCESS_SCRIPT")

    def test_no_httpx_import(self) -> None:
        """SubprocessExecutor does not import httpx."""
        from pathlib import Path

        from cloud_agents.workflow.executor.step import subprocess_exec as mod

        source = Path(mod.__file__).read_text()
        assert "import httpx" not in source
        assert "from httpx" not in source

    def test_anthropic_works_natively(self, mocker: MockerFixture) -> None:
        """Anthropic provider is no longer rejected (pydantic-ai handles it)."""
        from pathlib import Path

        from cloud_agents.workflow.executor.step import subprocess_exec as mod

        source = Path(mod.__file__).read_text()
        assert "non-OpenAI-compatible" not in source
        assert "_UNSUPPORTED_NATIVE_PROVIDERS" not in source
