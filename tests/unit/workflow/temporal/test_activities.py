"""Unit tests for Temporal sandbox activities (TDD)."""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from agents.workflow.temporal_activities import (
    build_escalation_activity,
    compute_pod_name,
    run_sandbox_step,
)


class TestComputePodName:
    """Tests for content-hash pod naming."""

    def test_same_input_same_name(self) -> None:
        """Identical inputs produce identical pod names."""
        name_a = compute_pod_name("wf-1", "step1", 1)
        name_b = compute_pod_name("wf-1", "step1", 1)
        assert name_a == name_b

    def test_different_input_different_name(self) -> None:
        """Different inputs produce different pod names."""
        name_a = compute_pod_name("wf-1", "step1", 1)
        name_b = compute_pod_name("wf-1", "step1", 2)
        assert name_a != name_b

    def test_name_has_prefix(self) -> None:
        """Pod name starts with ca- prefix."""
        name = compute_pod_name("wf-1", "step1", 1)
        assert name.startswith("ca-")


class TestRunSandboxStep:
    """Tests for the sandbox step activity."""

    @pytest.mark.asyncio
    async def test_success_returns_completed(self, mocker: MockerFixture) -> None:
        """Successful sandbox call returns completed status."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "output": {"summary": "diagnosed ok"},
        }

        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        result = await run_sandbox_step({
            "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        assert result["status"] == "completed"
        assert result["output"]["summary"] == "diagnosed ok"
        mock_spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_502_raises_for_retry(self, mocker: MockerFixture) -> None:
        """HTTP 502 from sandbox raises exception for Temporal retry."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 502

        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Infrastructure error"):
            await run_sandbox_step({
                "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
                "sandbox_image": "sandbox:latest",
                "context": {},
            }, spawner=mock_spawner)

        mock_spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_app_failure_returns_failed(self, mocker: MockerFixture) -> None:
        """HTTP 200 with success=false returns failed status."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "error": "agent failed",
        }

        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        result = await run_sandbox_step({
            "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        assert result["status"] == "failed"
        assert result["error"] == "agent failed"
        mock_spawner.destroy.assert_called_once()


    @pytest.mark.asyncio
    async def test_context_includes_prior_steps(self, mocker: MockerFixture) -> None:
        """Prior step results are passed to build_sandbox_context."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {"ok": True}}

        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_build_ctx = mocker.patch(
            "agents.workflow.temporal_activities.build_sandbox_context",
            return_value={},
        )

        await run_sandbox_step({
            "step": {"name": "exec", "prompt": "fix", "output_key": "r2",
                     "role": "execution", "execution_step": "r1"},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {
                "r1": {"status": "completed", "output": {"summary": "found issue"}},
            },
        }, spawner=mock_spawner)

        call_args = mock_build_ctx.call_args
        workflow_steps = call_args.kwargs.get("workflow_steps") or call_args[0][0]
        assert "r1" in workflow_steps
        assert workflow_steps["r1"].status == "completed"

    @pytest.mark.asyncio
    async def test_readiness_timeout_raises(self, mocker: MockerFixture) -> None:
        """Readiness timeout raises RuntimeError for Temporal retry."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = False

        with pytest.raises(RuntimeError, match="never became ready"):
            await run_sandbox_step({
                "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
                "sandbox_image": "sandbox:latest",
                "context": {},
            }, spawner=mock_spawner)

        mock_spawner.destroy.assert_called_once()


class TestBuildEscalation:
    """Tests for escalation activity."""

    @pytest.mark.asyncio
    async def test_packages_failed_steps(self) -> None:
        """Escalation packages failed step info."""
        result = await build_escalation_activity({
            "r1": {"status": "completed", "output": {"ok": True}},
            "r2": {"status": "failed", "error": "timeout"},
        })
        assert result["status"] == "escalated"
        assert len(result["output"]["failed_steps"]) == 1
        assert result["output"]["failed_steps"][0]["step"] == "r2"
