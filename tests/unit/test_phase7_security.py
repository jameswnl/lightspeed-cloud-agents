"""Unit tests for Phase 7 security hardening."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agents.remote_agent_client import RemoteAgentClient
from agents.spawner.base import SecretKeyRef, SpawnConfig
from agents.workflow.auto_approve import ApprovalPolicy, classify_step_risk
from agents.workflow.definition import WorkflowStepSpec
from agents.workflow.state import StepResult, WorkflowState


class TestSecretKeyRef:
    """Tests for SecretKeyRef model."""

    def test_create(self) -> None:
        """Test creating a SecretKeyRef."""
        ref = SecretKeyRef(secret_name="llm-api-key", key="OPENAI_API_KEY")
        assert ref.secret_name == "llm-api-key"
        assert ref.key == "OPENAI_API_KEY"

    def test_used_in_spawner_config(self) -> None:
        """Test that SecretKeyRef can be used in spawner configuration."""
        refs = {
            "OPENAI_API_KEY": SecretKeyRef(secret_name="llm-api-key", key="OPENAI_API_KEY"),
            "AGENT_API_TOKEN": SecretKeyRef(secret_name="agent-token", key="token"),
        }
        assert len(refs) == 2
        assert refs["OPENAI_API_KEY"].secret_name == "llm-api-key"


class TestRemoteAgentClientAuth:
    """Tests for auth_token on RemoteAgentClient."""

    def test_no_auth_by_default(self) -> None:
        """Test that no auth token is set by default."""
        client = RemoteAgentClient("http://agent:8080")
        assert client._auth_token is None

    def test_auth_token_stored(self) -> None:
        """Test that auth token is stored when provided."""
        client = RemoteAgentClient("http://agent:8080", auth_token="secret-token")
        assert client._auth_token == "secret-token"

    @pytest.mark.asyncio
    async def test_auth_header_sent(self) -> None:
        """Test that Bearer header is sent when auth_token is set."""
        import httpx
        from unittest.mock import patch, MagicMock

        client = RemoteAgentClient("http://agent:8080", auth_token="test-bearer")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "output": {"summary": "ok"},
            "output_type": "str",
            "schema_version": "v1",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "agent_name": "test",
            "success": True,
        }

        with patch("agents.remote_agent_client.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            await client.run("test prompt")

            call_kwargs = mock_http.post.call_args
            headers = call_kwargs[1].get("headers", {})
            assert headers.get("Authorization") == "Bearer test-bearer"

    @pytest.mark.asyncio
    async def test_no_auth_header_without_token(self) -> None:
        """Test that no Authorization header is sent without auth_token."""
        import httpx
        from unittest.mock import patch, MagicMock

        client = RemoteAgentClient("http://agent:8080")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "output": {},
            "output_type": "str",
            "schema_version": "v1",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "agent_name": "test",
            "success": True,
        }

        with patch("agents.remote_agent_client.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            await client.run("test prompt")

            call_kwargs = mock_http.post.call_args
            headers = call_kwargs[1].get("headers", {})
            assert "Authorization" not in headers


class TestExplicitRiskLevel:
    """Tests for fail-closed risk_level behavior."""

    def test_explicit_low_used(self) -> None:
        """Test explicit risk_level is used directly."""
        step = WorkflowStepSpec(
            name="check", type="agent", prompt="check", output_key="r",
            spawn="pre-deployed", risk_level="low",
        )
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "low"

    def test_missing_risk_defaults_high(self) -> None:
        """Test fail-closed: no risk_level → high risk."""
        step = WorkflowStepSpec(
            name="anything", type="agent", prompt="do stuff", output_key="r",
            spawn="pre-deployed",
        )
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "high"
        assert result.auto_approved is False

    def test_explicit_overrides_misleading_name(self) -> None:
        """Test explicit risk_level overrides step name that suggests otherwise."""
        step = WorkflowStepSpec(
            name="safe-looking-delete", type="agent", prompt="delete everything",
            output_key="r", spawn="pre-deployed", risk_level="low",
        )
        result = classify_step_risk(step, ApprovalPolicy())
        assert result.risk_level == "low"
