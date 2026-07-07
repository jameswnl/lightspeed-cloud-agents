"""Unit tests for runner-to-sandbox auth wiring in temporal_activities.py.

Validates that when SANDBOX_AUTH_ENABLED=true:
1. AGENT_API_TOKEN is injected into sandbox env vars
2. Authorization: Bearer header is sent on httpx POST
3. Auth is skipped when SANDBOX_AUTH_ENABLED is false/unset
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pytest_mock import MockerFixture

from cloud_agents.workflow.temporal_activities import run_sandbox_step


def _make_input() -> dict[str, Any]:
    """Build a standard sandbox step input dict."""
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


def _mock_http_success(mocker: MockerFixture) -> Any:
    """Set up httpx.AsyncClient mock returning success=True."""
    mock_response = mocker.MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "success": True,
        "output": {"summary": "ok"},
    }

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
    return mock_http, mock_client_instance


class TestSandboxAuthWiring:
    """Tests for runner-to-sandbox auth token injection."""

    @pytest.mark.asyncio
    async def test_auth_enabled_injects_token_env_var(self, mocker: MockerFixture) -> None:
        """SANDBOX_AUTH_ENABLED=true injects AGENT_API_TOKEN into sandbox env vars."""
        mocker.patch.dict(
            "os.environ",
            {"SANDBOX_AUTH_ENABLED": "true", "AGENT_API_TOKEN": "test-runner-token"},
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("AGENT_API_TOKEN") == "test-runner-token"

    @pytest.mark.asyncio
    async def test_auth_enabled_sends_bearer_header(self, mocker: MockerFixture) -> None:
        """SANDBOX_AUTH_ENABLED=true sends Authorization header on httpx POST."""
        mocker.patch.dict(
            "os.environ",
            {"SANDBOX_AUTH_ENABLED": "true", "AGENT_API_TOKEN": "test-runner-token"},
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _, mock_client_instance = _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        post_call = mock_client_instance.post.call_args
        headers = post_call[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer test-runner-token"

    @pytest.mark.asyncio
    async def test_auth_disabled_no_token_env_var(self, mocker: MockerFixture) -> None:
        """SANDBOX_AUTH_ENABLED unset does not inject AGENT_API_TOKEN."""
        mocker.patch.dict("os.environ", {}, clear=False)
        os.environ.pop("SANDBOX_AUTH_ENABLED", None)

        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert "AGENT_API_TOKEN" not in env_vars

    @pytest.mark.asyncio
    async def test_auth_disabled_no_bearer_header(self, mocker: MockerFixture) -> None:
        """SANDBOX_AUTH_ENABLED unset does not send Authorization header."""
        mocker.patch.dict("os.environ", {}, clear=False)
        os.environ.pop("SANDBOX_AUTH_ENABLED", None)

        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _, mock_client_instance = _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        post_call = mock_client_instance.post.call_args
        headers = post_call[1].get("headers") or {}
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_auth_enabled_false_no_auth(self, mocker: MockerFixture) -> None:
        """SANDBOX_AUTH_ENABLED=false does not inject auth."""
        mocker.patch.dict(
            "os.environ",
            {"SANDBOX_AUTH_ENABLED": "false", "AGENT_API_TOKEN": "test-runner-token"},
        )
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _, mock_client_instance = _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert "AGENT_API_TOKEN" not in env_vars

        post_call = mock_client_instance.post.call_args
        headers = post_call[1].get("headers") or {}
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_auth_sa_token_mode(self, mocker: MockerFixture) -> None:
        """SA token mode reads token from projected volume path."""
        mocker.patch.dict(
            "os.environ",
            {"SANDBOX_AUTH_ENABLED": "true", "AUTH_MODE": "sa_token"},
        )
        mocker.patch(
            "builtins.open",
            mocker.mock_open(read_data="sa-projected-token\n"),
        )

        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _, mock_client_instance = _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert env_vars.get("AGENT_API_TOKEN") == "sa-projected-token"

        post_call = mock_client_instance.post.call_args
        headers = post_call[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer sa-projected-token"

    @pytest.mark.asyncio
    async def test_auth_enabled_no_token_available_skips_auth(
        self, mocker: MockerFixture
    ) -> None:
        """SANDBOX_AUTH_ENABLED=true but no token available skips auth gracefully."""
        mocker.patch.dict(
            "os.environ",
            {"SANDBOX_AUTH_ENABLED": "true"},
        )
        os.environ.pop("AGENT_API_TOKEN", None)
        os.environ.pop("AGENT_API_TOKENS", None)

        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        _, mock_client_instance = _mock_http_success(mocker)

        await run_sandbox_step(_make_input(), spawner=mock_spawner)

        spawn_call = mock_spawner.spawn.call_args
        env_vars = spawn_call[1].get("env", {})
        assert "AGENT_API_TOKEN" not in env_vars

        post_call = mock_client_instance.post.call_args
        headers = post_call[1].get("headers") or {}
        assert "Authorization" not in headers
