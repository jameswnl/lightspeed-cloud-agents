"""Tests for the Temporal-free step runner module."""

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


class TestRunStep:
    """Tests for the extracted run_step function."""

    @pytest.fixture(name="mock_spawner")
    def mock_spawner_fixture(self, mocker: MockerFixture) -> AsyncMock:
        """Create a mock spawner."""
        spawner = mocker.AsyncMock()
        spawner.spawn.return_value = "http://pod-1:8080"
        spawner.wait_ready.return_value = True
        return spawner

    @pytest.fixture(name="mock_http_success")
    def mock_http_success_fixture(self, mocker: MockerFixture) -> None:
        """Mock httpx to return a successful agent response."""
        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {"summary": "done"}}

        mock_transcript_response = mocker.MagicMock()
        mock_transcript_response.status_code = 404

        mock_client = mocker.MagicMock()
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mock_client.get = mocker.AsyncMock(return_value=mock_transcript_response)

        mock_http = mocker.patch("cloud_agents.workflow.step_runner.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

    @pytest.fixture(name="step_input")
    def step_input_fixture(self) -> dict[str, Any]:
        """Standard step input dict."""
        return {
            "step": {
                "name": "diagnose",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
            "workflow_id": "wf-test-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

    @pytest.mark.asyncio
    async def test_run_step_returns_completed(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Successful step returns completed status with output."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)
        assert result["status"] == "completed"
        assert result["output"]["summary"] == "done"

    @pytest.mark.asyncio
    async def test_run_step_no_temporal_imports(self) -> None:
        """step_runner module has zero temporalio imports."""
        import cloud_agents.workflow.step_runner as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source
        assert "@activity.defn" not in source

    @pytest.mark.asyncio
    async def test_run_step_spawns_sandbox(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Step runner calls spawner.spawn with correct args."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)

        mock_spawner.spawn.assert_called_once()
        call_kwargs = mock_spawner.spawn.call_args[1]
        assert call_kwargs["env"]["LIGHTSPEED_PROVIDER"] == "openai"
        assert call_kwargs["env"]["LIGHTSPEED_MODEL"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_run_step_no_spawner_returns_stub(
        self, step_input: dict[str, Any]
    ) -> None:
        """Without a spawner, returns a stub result."""
        from cloud_agents.workflow.step_runner import run_step

        result = await run_step(step_input, spawner=None, attempt=1)
        assert result["status"] == "completed"
        assert "diagnose" in result["output"]["summary"]

    @pytest.mark.asyncio
    async def test_run_step_attempt_in_pod_name(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Attempt number affects the pod name (content-hash)."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)
        pod_name_1 = mock_spawner.spawn.call_args[0][0]

        mock_spawner.reset_mock()
        await run_step(step_input, spawner=mock_spawner, attempt=2)
        pod_name_2 = mock_spawner.spawn.call_args[0][0]

        assert pod_name_1 != pod_name_2

    @pytest.mark.asyncio
    async def test_run_step_destroys_sandbox(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Sandbox is destroyed after step completes."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)
        mock_spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_step_http_502_raises(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """HTTP 502 from sandbox raises RuntimeError for retry."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_response = mocker.MagicMock()
        mock_response.status_code = 502

        mock_client = mocker.MagicMock()
        mock_client.post = mocker.AsyncMock(return_value=mock_response)

        mock_http = mocker.patch("cloud_agents.workflow.step_runner.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        from cloud_agents.workflow.step_runner import run_step

        with pytest.raises(RuntimeError, match="Infrastructure error"):
            await run_step(step_input, spawner=mock_spawner, attempt=1)

    @pytest.mark.asyncio
    async def test_run_step_circuit_breaker_open(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Circuit breaker open returns failed without spawning."""
        mocker.patch(
            "cloud_agents.workflow.step_runner._circuit_breaker.is_open",
            return_value=True,
        )

        from cloud_agents.workflow.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)
        assert result["status"] == "failed"
        assert "Circuit breaker" in result["error"]
        mock_spawner.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_step_not_ready_raises(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Sandbox not becoming ready raises RuntimeError."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_spawner.wait_ready.return_value = False

        from cloud_agents.workflow.step_runner import run_step

        with pytest.raises(RuntimeError, match="never became ready"):
            await run_step(step_input, spawner=mock_spawner, attempt=1)

    @pytest.mark.asyncio
    async def test_run_step_agent_failure_returns_failed(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Agent returning success=false produces failed status."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "error": "agent failed",
        }
        mock_transcript = mocker.MagicMock()
        mock_transcript.status_code = 404

        mock_client = mocker.MagicMock()
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mock_client.get = mocker.AsyncMock(return_value=mock_transcript)

        mock_http = mocker.patch("cloud_agents.workflow.step_runner.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        from cloud_agents.workflow.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)
        assert result["status"] == "failed"
        assert result["error"] == "agent failed"

    @pytest.mark.asyncio
    async def test_run_step_credential_env_key_normalization(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        mocker: MockerFixture,
    ) -> None:
        """Credential lookup normalizes K8s secret name to env var format."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}, clear=False)

        from cloud_agents.workflow.step_runner import run_step

        input_data = {
            "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

        await run_step(input_data, spawner=mock_spawner, attempt=1)

        call_kwargs = mock_spawner.spawn.call_args[1]
        env = call_kwargs["env"]
        assert "OPENAI_API_KEY" in env
        assert env["OPENAI_API_KEY"] == "sk-test-key"
        assert "openai-api-key" not in env
