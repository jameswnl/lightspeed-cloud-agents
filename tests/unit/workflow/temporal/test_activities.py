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


    @pytest.mark.asyncio
    async def test_permissions_service_account_passed(self, mocker: MockerFixture) -> None:
        """Permissions service_account is forwarded to spawner."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step({
            "step": {"name": "s1", "prompt": "check", "output_key": "r1",
                     "permissions": {"service_account": "custom-sa"}},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("LIGHTSPEED_SERVICE_ACCOUNT") == "custom-sa"

    @pytest.mark.asyncio
    async def test_permissions_timeout_overrides_default(self, mocker: MockerFixture) -> None:
        """Permissions timeout_seconds overrides default HTTP timeout."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

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

        await run_sandbox_step({
            "step": {"name": "s1", "prompt": "check", "output_key": "r1",
                     "permissions": {"timeout_seconds": 120}},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        http_init_call = mock_http.call_args
        assert http_init_call[1].get("timeout") == 120.0


class TestNotificationActivity:
    """Tests for approval notification activity."""

    @pytest.mark.asyncio
    async def test_notification_sends_with_correlation_id(
        self, mocker: MockerFixture,
    ) -> None:
        """Notification includes correlation_id and calls notifier."""
        from agents.workflow.temporal_activities import send_approval_notification

        mock_notifier_cls = mocker.patch(
            "agents.workflow.temporal_activities.NullNotifier",
        )
        mock_notifier = mocker.AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await send_approval_notification({
            "workflow_id": "wf-1",
            "step_name": "approve",
            "message": "Please approve",
            "notifier_config": None,
        })

        mock_notifier.notify.assert_called_once()
        call_kwargs = mock_notifier.notify.call_args[1]
        assert "wf-1:approve" in call_kwargs["message"]

    @pytest.mark.asyncio
    async def test_notification_failure_non_fatal(
        self, mocker: MockerFixture,
    ) -> None:
        """Notification failure does not raise."""
        from agents.workflow.temporal_activities import send_approval_notification

        mock_notifier_cls = mocker.patch(
            "agents.workflow.temporal_activities.NullNotifier",
        )
        mock_notifier = mocker.AsyncMock()
        mock_notifier.notify.side_effect = RuntimeError("webhook failed")
        mock_notifier_cls.return_value = mock_notifier

        result = await send_approval_notification({
            "workflow_id": "wf-1",
            "step_name": "approve",
            "message": "Please approve",
            "notifier_config": None,
        })

        assert result["status"] == "notification_failed"


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

    @pytest.mark.asyncio
    async def test_escalation_delivery_failure_non_fatal(
        self, mocker: MockerFixture,
    ) -> None:
        """Escalation packager failure is non-fatal; artifact still returned."""
        mock_packager_cls = mocker.patch(
            "agents.workflow.temporal_activities.LogPackager",
        )
        mock_packager = mocker.AsyncMock()
        mock_packager.package.side_effect = RuntimeError("delivery failed")
        mock_packager_cls.return_value = mock_packager

        result = await build_escalation_activity({
            "r1": {"status": "failed", "error": "timeout"},
        })

        assert result["status"] == "escalated"
        assert result["output"]["failed_steps"][0]["step"] == "r1"
