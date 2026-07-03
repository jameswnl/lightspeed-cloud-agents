"""Unit tests for Temporal sandbox activities (TDD)."""

from __future__ import annotations

import os

import pytest
from pytest_mock import MockerFixture

from cloud_agents.workflow.temporal_activities import (
    _normalize_config_ref,
    build_escalation_activity,
    compute_pod_name,
    run_sandbox_step,
    send_approval_notification,
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
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        result = await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

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
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Infrastructure error"):
            await run_sandbox_step(
                {
                    "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                    "workflow_id": "wf-1",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4",
                        "credentials_secret": "k",
                    },
                    "sandbox_image": "sandbox:latest",
                    "context": {},
                },
                spawner=mock_spawner,
            )

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
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        result = await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

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
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        mock_build_ctx = mocker.patch(
            "cloud_agents.workflow.temporal_activities.build_sandbox_context",
            return_value={},
        )

        await run_sandbox_step(
            {
                "step": {
                    "name": "exec",
                    "prompt": "fix",
                    "output_key": "r2",
                    "role": "execution",
                    "execution_step": "r1",
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {
                    "r1": {"status": "completed", "output": {"summary": "found issue"}},
                },
            },
            spawner=mock_spawner,
        )

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
            await run_sandbox_step(
                {
                    "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                    "workflow_id": "wf-1",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4",
                        "credentials_secret": "k",
                    },
                    "sandbox_image": "sandbox:latest",
                    "context": {},
                },
                spawner=mock_spawner,
            )

        mock_spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_permissions_service_account_passed(
        self, mocker: MockerFixture
    ) -> None:
        """Permissions service_account is forwarded to spawner."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "check",
                    "output_key": "r1",
                    "permissions": {"service_account": "custom-sa"},
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("LIGHTSPEED_SERVICE_ACCOUNT") == "custom-sa"

    @pytest.mark.asyncio
    async def test_permissions_timeout_overrides_default(
        self, mocker: MockerFixture
    ) -> None:
        """Permissions timeout_seconds overrides default HTTP timeout."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "check",
                    "output_key": "r1",
                    "permissions": {"timeout_seconds": 120},
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        http_init_call = mock_http.call_args
        assert http_init_call[1].get("timeout") == 120.0


class TestNotificationActivity:
    """Tests for approval notification activity."""

    @pytest.mark.asyncio
    async def test_notification_sends_with_correlation_id(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Notification includes correlation_id and calls notifier."""
        from cloud_agents.workflow.temporal_activities import send_approval_notification

        mock_notifier_cls = mocker.patch(
            "cloud_agents.workflow.temporal_activities.NullNotifier",
        )
        mock_notifier = mocker.AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await send_approval_notification(
            {
                "workflow_id": "wf-1",
                "step_name": "approve",
                "message": "Please approve",
                "notifier_config": None,
            }
        )

        mock_notifier.notify.assert_called_once()
        call_kwargs = mock_notifier.notify.call_args[1]
        assert "wf-1:approve" in call_kwargs["message"]

    @pytest.mark.asyncio
    async def test_notification_failure_non_fatal(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Notification failure does not raise."""
        from cloud_agents.workflow.temporal_activities import send_approval_notification

        mock_notifier_cls = mocker.patch(
            "cloud_agents.workflow.temporal_activities.NullNotifier",
        )
        mock_notifier = mocker.AsyncMock()
        mock_notifier.notify.side_effect = RuntimeError("webhook failed")
        mock_notifier_cls.return_value = mock_notifier

        result = await send_approval_notification(
            {
                "workflow_id": "wf-1",
                "step_name": "approve",
                "message": "Please approve",
                "notifier_config": None,
            }
        )

        assert result["status"] == "notification_failed"


class TestBuildEscalation:
    """Tests for escalation activity."""

    @pytest.mark.asyncio
    async def test_packages_failed_steps(self) -> None:
        """Escalation packages failed step info."""
        result = await build_escalation_activity(
            {
                "r1": {"status": "completed", "output": {"ok": True}},
                "r2": {"status": "failed", "error": "timeout"},
            }
        )
        assert result["status"] == "escalated"
        assert len(result["output"]["failed_steps"]) == 1
        assert result["output"]["failed_steps"][0]["step"] == "r2"

    @pytest.mark.asyncio
    async def test_escalation_delivery_failure_non_fatal(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Escalation packager failure is non-fatal; artifact still returned."""
        mock_packager_cls = mocker.patch(
            "cloud_agents.workflow.temporal_activities.LogPackager",
        )
        mock_packager = mocker.AsyncMock()
        mock_packager.package.side_effect = RuntimeError("delivery failed")
        mock_packager_cls.return_value = mock_packager

        result = await build_escalation_activity(
            {
                "r1": {"status": "failed", "error": "timeout"},
            }
        )

        assert result["status"] == "escalated"
        assert result["output"]["failed_steps"][0]["step"] == "r1"


class TestNormalizeConfigRef:
    """Tests for config ref normalization."""

    def test_hyphens_to_underscores(self) -> None:
        """Hyphens become underscores."""
        assert (
            _normalize_config_ref("slack-approval-channel") == "SLACK_APPROVAL_CHANNEL"
        )

    def test_already_uppercase(self) -> None:
        """Already uppercase passes through."""
        assert _normalize_config_ref("DEFAULT") == "DEFAULT"

    def test_dots_and_special_chars(self) -> None:
        """Dots and special chars become underscores."""
        assert _normalize_config_ref("my.config.ref") == "MY_CONFIG_REF"


class TestNotificationConfigResolution:
    """Tests for notifier config-ref env var resolution."""

    @pytest.mark.asyncio
    async def test_slack_notifier_resolved_from_env(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Slack notifier resolves webhook URL from env var."""
        mocker.patch.dict(
            "os.environ",
            {
                "NOTIFIER_SLACK_APPROVAL_CHANNEL_WEBHOOK_URL": "https://hooks.slack.com/test"
            },
        )
        mock_slack = mocker.patch(
            "cloud_agents.workflow.notifier.SlackNotifier",
        )
        mock_instance = mocker.AsyncMock()
        mock_slack.return_value = mock_instance

        await send_approval_notification(
            {
                "workflow_id": "wf-1",
                "step_name": "approve",
                "message": "OK?",
                "notifier_config": {"type": "slack", "config_ref": "approval-channel"},
            }
        )

        mock_slack.assert_called_once_with(webhook_url="https://hooks.slack.com/test")

    @pytest.mark.asyncio
    async def test_webhook_notifier_resolved_from_env(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Webhook notifier resolves URL from env var."""
        mocker.patch.dict(
            "os.environ",
            {"NOTIFIER_WEBHOOK_MY_ENDPOINT_URL": "https://example.com/notify"},
        )
        mock_webhook = mocker.patch(
            "cloud_agents.workflow.notifier.WebhookNotifier",
        )
        mock_instance = mocker.AsyncMock()
        mock_webhook.return_value = mock_instance

        await send_approval_notification(
            {
                "workflow_id": "wf-1",
                "step_name": "approve",
                "message": "OK?",
                "notifier_config": {"type": "webhook", "config_ref": "my-endpoint"},
            }
        )

        mock_webhook.assert_called_once_with(url="https://example.com/notify")


class TestAdvisorySpawnerEnforcement:
    """Tests for advisory mode enforcement at spawner level."""

    @pytest.mark.asyncio
    async def test_advisory_sets_advisory_sa(self, mocker: MockerFixture) -> None:
        """Advisory mode sets service_account to advisory-sa."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "diag",
                    "prompt": "check",
                    "output_key": "r1",
                    "advisory": True,
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        assert spawn_call[1].get("service_account") == "advisory-sa"
        assert spawn_call[1].get("read_only") is True

    @pytest.mark.asyncio
    async def test_non_advisory_no_read_only(self, mocker: MockerFixture) -> None:
        """Non-advisory mode does not set read_only."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        assert spawn_call[1].get("read_only") is False

    @pytest.mark.asyncio
    async def test_deployment_env_vars_forwarded(self, mocker: MockerFixture) -> None:
        """Deployment env vars (LIGHTSPEED_PROVIDER_URL etc.) forwarded to sandbox."""
        mocker.patch.dict(
            "os.environ",
            {
                "LIGHTSPEED_PROVIDER_URL": "https://api.openai.com/v1",
                "LIGHTSPEED_MODEL_PROVIDER": "openai",
            },
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("LIGHTSPEED_PROVIDER_URL") == "https://api.openai.com/v1"
        assert env_vars.get("LIGHTSPEED_MODEL_PROVIDER") == "openai"

    @pytest.mark.asyncio
    async def test_skills_forwarded_to_spawner(self, mocker: MockerFixture) -> None:
        """Skills image and paths are forwarded to spawner.spawn()."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "skills_image": "skills:v1",
                "skills_paths": ["/skills/diag"],
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        assert spawn_call[1].get("skills_image") == "skills:v1"
        assert spawn_call[1].get("skills_paths") == ["/skills/diag"]


class TestMCPInjection:
    """Tests for MCP server config injection into sandbox env vars."""

    @pytest.mark.asyncio
    async def test_mcp_servers_set_as_env_var(self, mocker: MockerFixture) -> None:
        """MCP server config serialized as LIGHTSPEED_MCP_SERVERS env var."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
                "mcp_servers": [
                    {
                        "name": "sn",
                        "url": "http://mcp.local/sse",
                        "headers": {"X-Custom": "val"},
                    }
                ],
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        import json

        mcp_json = json.loads(env_vars.get("LIGHTSPEED_MCP_SERVERS", "[]"))
        assert len(mcp_json) == 1
        assert mcp_json[0]["name"] == "sn"
        assert mcp_json[0]["url"] == "http://mcp.local/sse"
        assert mcp_json[0]["headers"]["X-Custom"] == "val"

    @pytest.mark.asyncio
    async def test_mcp_servers_not_set_when_absent(self, mocker: MockerFixture) -> None:
        """LIGHTSPEED_MCP_SERVERS not set when no mcp_servers provided."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert "LIGHTSPEED_MCP_SERVERS" not in env_vars

    @pytest.mark.asyncio
    async def test_secret_headers_encoded_as_file_refs(
        self, mocker: MockerFixture
    ) -> None:
        """Secret headers encoded as file references in MCP env var."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
                "mcp_servers": [
                    {
                        "name": "servicenow",
                        "url": "http://mcp.local/sse",
                        "secret_headers": {
                            "Authorization": {
                                "secret_name": "mcp-sn-token",
                                "key": "bearer-token",
                            },
                        },
                    }
                ],
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        import json

        mcp_json = json.loads(env_vars["LIGHTSPEED_MCP_SERVERS"])
        server = mcp_json[0]
        assert server["headers"]["Authorization"] == {
            "file": "/var/secrets/mcp/servicenow/bearer-token"
        }

    @pytest.mark.asyncio
    async def test_mcp_secret_mounts_passed_to_spawner(
        self, mocker: MockerFixture
    ) -> None:
        """MCP secret refs passed to spawner as mcp_secret_mounts."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
                "mcp_servers": [
                    {
                        "name": "servicenow",
                        "url": "http://mcp.local/sse",
                        "secret_headers": {
                            "Authorization": {
                                "secret_name": "mcp-sn-token",
                                "key": "bearer-token",
                            },
                        },
                    }
                ],
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        mcp_mounts = spawn_call[1].get("mcp_secret_mounts")
        assert mcp_mounts is not None
        assert len(mcp_mounts) == 1
        assert mcp_mounts[0] == (
            "mcp-sn-token",
            "bearer-token",
            "/var/secrets/mcp/servicenow/",
        )

    @pytest.mark.asyncio
    async def test_mcp_allowed_secrets_blocks_unlisted(
        self, mocker: MockerFixture
    ) -> None:
        """MCP_ALLOWED_SECRETS rejects secrets not in the allowlist."""
        mocker.patch.dict(
            "os.environ",
            {"MCP_ALLOWED_SECRETS": "allowed-secret"},
        )
        mock_spawner = mocker.AsyncMock()

        with pytest.raises(ValueError, match="not in MCP_ALLOWED_SECRETS"):
            await run_sandbox_step(
                {
                    "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                    "workflow_id": "wf-1",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4",
                        "credentials_secret": "k",
                    },
                    "sandbox_image": "sandbox:latest",
                    "context": {},
                    "mcp_servers": [
                        {
                            "name": "sn",
                            "url": "http://mcp.local/sse",
                            "secret_headers": {
                                "Authorization": {
                                    "secret_name": "blocked-secret",
                                    "key": "token",
                                },
                            },
                        }
                    ],
                },
                spawner=mock_spawner,
            )

    @pytest.mark.asyncio
    async def test_mcp_allowed_secrets_permits_listed(
        self, mocker: MockerFixture
    ) -> None:
        """MCP_ALLOWED_SECRETS permits secrets in the allowlist."""
        mocker.patch.dict(
            "os.environ",
            {"MCP_ALLOWED_SECRETS": "mcp-sn-token,other-secret"},
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        result = await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
                "mcp_servers": [
                    {
                        "name": "sn",
                        "url": "http://mcp.local/sse",
                        "secret_headers": {
                            "Authorization": {
                                "secret_name": "mcp-sn-token",
                                "key": "bearer-token",
                            },
                        },
                    }
                ],
            },
            spawner=mock_spawner,
        )

        assert result["status"] == "completed"


class TestOutputSchemaForwarding:
    """Tests that output_schema is passed to the sandbox."""

    @pytest.mark.asyncio
    async def test_output_schema_sent_to_sandbox(self, mocker: MockerFixture) -> None:
        """output_schema from step config is included in sandbox POST body."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {"severity": "high"}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        schema = {
            "type": "object",
            "properties": {"severity": {"type": "string"}},
            "required": ["severity"],
        }

        await run_sandbox_step({
            "step": {
                "name": "diag",
                "prompt": "diagnose",
                "output_key": "r1",
                "output_schema": schema,
            },
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        post_call = mock_client_instance.post.call_args
        body = post_call[1]["json"]
        assert body["outputSchema"] == schema


class TestModelProviderDerivation:
    """Tests for per-workflow model_provider derivation."""

    @pytest.mark.asyncio
    async def test_model_provider_from_provider_config(
        self, mocker: MockerFixture
    ) -> None:
        """model_provider in ProviderConfig sets LIGHTSPEED_MODEL_PROVIDER env var on pod."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                    "model_provider": "anthropic",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("LIGHTSPEED_MODEL_PROVIDER") == "anthropic"

    @pytest.mark.asyncio
    async def test_model_provider_fallback_to_env(
        self, mocker: MockerFixture
    ) -> None:
        """No model_provider in config falls back to os.environ LIGHTSPEED_MODEL_PROVIDER."""
        mocker.patch.dict(
            "os.environ",
            {"LIGHTSPEED_MODEL_PROVIDER": "openai"},
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("LIGHTSPEED_MODEL_PROVIDER") == "openai"

    @pytest.mark.asyncio
    async def test_model_provider_overrides_env(
        self, mocker: MockerFixture
    ) -> None:
        """model_provider in config overrides os.environ value."""
        mocker.patch.dict(
            "os.environ",
            {"LIGHTSPEED_MODEL_PROVIDER": "openai"},
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                    "model_provider": "anthropic",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("LIGHTSPEED_MODEL_PROVIDER") == "anthropic"

    @pytest.mark.asyncio
    async def test_no_model_provider_anywhere(
        self, mocker: MockerFixture
    ) -> None:
        """No model_provider in config or env means not in env_vars."""
        mocker.patch.dict(
            "os.environ",
            {},
            clear=False,
        )
        # Ensure LIGHTSPEED_MODEL_PROVIDER is not in env
        os.environ.pop("LIGHTSPEED_MODEL_PROVIDER", None)

        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert "LIGHTSPEED_MODEL_PROVIDER" not in env_vars


class TestPermissionScopeForwarding:
    """Tests that allowed_tools/denied_tools are forwarded to sandbox POST body."""

    @pytest.mark.asyncio
    async def test_allowed_tools_forwarded_to_sandbox(
        self, mocker: MockerFixture
    ) -> None:
        """allowed_tools in step permissions -> allowedTools in sandbox POST body."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "test",
                    "output_key": "r1",
                    "permissions": {
                        "allowed_tools": ["list_hosts", "check_host"],
                    },
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        post_call = mock_client_instance.post.call_args
        body = post_call[1]["json"]
        assert body["allowedTools"] == ["list_hosts", "check_host"]

    @pytest.mark.asyncio
    async def test_denied_tools_forwarded_to_sandbox(
        self, mocker: MockerFixture
    ) -> None:
        """denied_tools in step permissions -> deniedTools in sandbox POST body."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "test",
                    "output_key": "r1",
                    "permissions": {
                        "denied_tools": ["run_remediation"],
                    },
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        post_call = mock_client_instance.post.call_args
        body = post_call[1]["json"]
        assert body["deniedTools"] == ["run_remediation"]

    @pytest.mark.asyncio
    async def test_both_allowed_and_denied_forwarded(
        self, mocker: MockerFixture
    ) -> None:
        """Both fields forwarded when both present."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "test",
                    "output_key": "r1",
                    "permissions": {
                        "allowed_tools": ["list_hosts"],
                        "denied_tools": ["run_remediation"],
                    },
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        post_call = mock_client_instance.post.call_args
        body = post_call[1]["json"]
        assert body["allowedTools"] == ["list_hosts"]
        assert body["deniedTools"] == ["run_remediation"]

    @pytest.mark.asyncio
    async def test_no_permissions_no_tool_fields(
        self, mocker: MockerFixture
    ) -> None:
        """No permissions -> no allowedTools/deniedTools in body."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "test",
                    "output_key": "r1",
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        post_call = mock_client_instance.post.call_args
        body = post_call[1]["json"]
        assert "allowedTools" not in body
        assert "deniedTools" not in body

    @pytest.mark.asyncio
    async def test_permissions_without_tools_no_tool_fields(
        self, mocker: MockerFixture
    ) -> None:
        """Permissions with only service_account -> no tool fields in body."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client_instance = mocker.MagicMock(
            post=mocker.AsyncMock(return_value=mock_response),
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mock_client_instance,
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "s1",
                    "prompt": "test",
                    "output_key": "r1",
                    "permissions": {"service_account": "sa"},
                },
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        post_call = mock_client_instance.post.call_args
        body = post_call[1]["json"]
        assert "allowedTools" not in body
        assert "deniedTools" not in body


class TestAuditEmission:
    """Tests for audit event emission from activities."""

    @pytest.mark.asyncio
    async def test_sandbox_spawned_audit_event(self, mocker: MockerFixture) -> None:
        """Successful sandbox step emits sandbox_spawned audit event."""
        mock_emit = mocker.patch("cloud_agents.workflow.temporal_activities.emit_audit")
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
                "workflow_id": "wf-audit-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        spawn_calls = [
            c
            for c in mock_emit.call_args_list
            if c[1].get("event_type") == "sandbox_spawned"
        ]
        assert len(spawn_calls) == 1
        assert spawn_calls[0][1]["workflow_id"] == "wf-audit-1"

        destroy_calls = [
            c
            for c in mock_emit.call_args_list
            if c[1].get("event_type") == "sandbox_destroyed"
        ]
        assert len(destroy_calls) == 1


class TestCircuitBreakerInActivity:
    """Tests for circuit breaker integration in sandbox activities."""

    @pytest.mark.asyncio
    async def test_open_breaker_returns_failed_without_spawning(
        self, mocker: MockerFixture
    ) -> None:
        """Open circuit breaker returns failed without spawning sandbox."""
        mock_cb = mocker.patch(
            "cloud_agents.workflow.temporal_activities._circuit_breaker"
        )
        mock_cb.is_open.return_value = True
        mock_spawner = mocker.AsyncMock()

        result = await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        assert result["status"] == "failed"
        assert "circuit breaker" in result["error"].lower()
        mock_spawner.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_records_on_breaker(self, mocker: MockerFixture) -> None:
        """Successful sandbox step records success on breaker."""
        mock_cb = mocker.patch(
            "cloud_agents.workflow.temporal_activities._circuit_breaker"
        )
        mock_cb.is_open.return_value = False
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        mock_cb.record_success.assert_called_with("openai")

    @pytest.mark.asyncio
    async def test_failure_records_on_breaker(self, mocker: MockerFixture) -> None:
        """Failed sandbox step records failure on breaker."""
        mock_cb = mocker.patch(
            "cloud_agents.workflow.temporal_activities._circuit_breaker"
        )
        mock_cb.is_open.return_value = False
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": False, "error": "agent failed"}

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
                "workflow_id": "wf-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        mock_cb.record_failure.assert_called_with("openai")

    @pytest.mark.asyncio
    async def test_http_502_records_failure_on_breaker(self, mocker: MockerFixture) -> None:
        """HTTP 502 from sandbox records failure on circuit breaker."""
        mock_cb = mocker.patch(
            "cloud_agents.workflow.temporal_activities._circuit_breaker"
        )
        mock_cb.is_open.return_value = False
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 502

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient"
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response)
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Infrastructure error"):
            await run_sandbox_step(
                {
                    "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
                    "workflow_id": "wf-1",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4",
                        "credentials_secret": "k",
                    },
                    "sandbox_image": "sandbox:latest",
                    "context": {},
                },
                spawner=mock_spawner,
            )

        mock_cb.record_failure.assert_called_with("openai")

    @pytest.mark.asyncio
    async def test_readiness_failure_records_on_breaker(self, mocker: MockerFixture) -> None:
        """Readiness timeout records failure on circuit breaker."""
        mock_cb = mocker.patch(
            "cloud_agents.workflow.temporal_activities._circuit_breaker"
        )
        mock_cb.is_open.return_value = False
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = False

        with pytest.raises(RuntimeError, match="never became ready"):
            await run_sandbox_step(
                {
                    "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
                    "workflow_id": "wf-1",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4",
                        "credentials_secret": "k",
                    },
                    "sandbox_image": "sandbox:latest",
                    "context": {},
                },
                spawner=mock_spawner,
            )

        mock_cb.record_failure.assert_called_with("openai")


class TestSkipSandboxDestroy:
    """Tests for SKIP_SANDBOX_DESTROY env var in the finally block."""

    def _make_success_input(self) -> dict:
        """Build a standard successful sandbox step input dict."""
        return {
            "step": {"name": "diag", "prompt": "diagnose", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4",
                "credentials_secret": "k",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

    def _mock_http_success(self, mocker: MockerFixture) -> None:
        """Set up httpx.AsyncClient mock returning success=True."""
        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "output": {"summary": "diagnosed ok"},
        }

        mock_http = mocker.patch(
            "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response),
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

    @pytest.mark.asyncio
    async def test_skip_destroy_when_env_set_true(
        self, mocker: MockerFixture
    ) -> None:
        """SKIP_SANDBOX_DESTROY=true skips spawner.destroy, result still returned."""
        mocker.patch.dict("os.environ", {"SKIP_SANDBOX_DESTROY": "true"})
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        self._mock_http_success(mocker)

        result = await run_sandbox_step(
            self._make_success_input(),
            spawner=mock_spawner,
        )

        assert result["status"] == "completed"
        assert result["output"]["summary"] == "diagnosed ok"
        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_destroy_when_env_set_1(
        self, mocker: MockerFixture
    ) -> None:
        """SKIP_SANDBOX_DESTROY=1 skips spawner.destroy, result still returned."""
        mocker.patch.dict("os.environ", {"SKIP_SANDBOX_DESTROY": "1"})
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        self._mock_http_success(mocker)

        result = await run_sandbox_step(
            self._make_success_input(),
            spawner=mock_spawner,
        )

        assert result["status"] == "completed"
        assert result["output"]["summary"] == "diagnosed ok"
        mock_spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_destroy_called_when_env_not_set(
        self, mocker: MockerFixture
    ) -> None:
        """Without SKIP_SANDBOX_DESTROY, spawner.destroy IS called."""
        mocker.patch.dict("os.environ", {}, clear=False)
        os.environ.pop("SKIP_SANDBOX_DESTROY", None)

        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        self._mock_http_success(mocker)

        result = await run_sandbox_step(
            self._make_success_input(),
            spawner=mock_spawner,
        )

        assert result["status"] == "completed"
        mock_spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_destroy_case_insensitive(
        self, mocker: MockerFixture
    ) -> None:
        """SKIP_SANDBOX_DESTROY=True (capital T) also skips destroy."""
        mocker.patch.dict("os.environ", {"SKIP_SANDBOX_DESTROY": "True"})
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        self._mock_http_success(mocker)

        result = await run_sandbox_step(
            self._make_success_input(),
            spawner=mock_spawner,
        )

        assert result["status"] == "completed"
        assert result["output"]["summary"] == "diagnosed ok"
        mock_spawner.destroy.assert_not_called()
