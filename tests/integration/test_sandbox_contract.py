"""Integration tests for sandbox HTTP contract.

Validates the request/response shapes between the Temporal activity
and the sandbox /v1/agent/run endpoint using a mock HTTP server.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture

from agents.workflow.temporal_activities import run_sandbox_step


class TestSandboxRequestContract:
    """Tests that run_sandbox_step sends correctly shaped requests."""

    @pytest.mark.asyncio
    async def test_request_body_matches_contract(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Request body contains query, context, and optional fields."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://sandbox:8080"
        mock_spawner.wait_ready.return_value = True

        captured_request: dict[str, Any] = {}

        async def capture_post(url, json=None, **kwargs):
            captured_request.update(json or {})
            captured_request["_url"] = url
            resp = mocker.MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True, "output": {"summary": "ok"}}
            return resp

        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient",
        )
        mock_client = mocker.MagicMock(post=mocker.AsyncMock(side_effect=capture_post))
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        await run_sandbox_step(
            {
                "step": {
                    "name": "diag",
                    "prompt": "check cluster health",
                    "output_key": "r1",
                    "instructions": "You are a K8s expert",
                    "output_schema": {"type": "object"},
                },
                "workflow_id": "wf-contract-1",
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "key",
                },
                "sandbox_image": "sandbox:latest",
                "context": {},
            },
            spawner=mock_spawner,
        )

        assert captured_request["_url"] == "http://sandbox:8080/v1/agent/run"
        assert "query" in captured_request
        assert captured_request["query"] == "check cluster health"
        assert "context" in captured_request
        assert captured_request["systemPrompt"] == "You are a K8s expert"
        assert captured_request["outputSchema"] == {"type": "object"}


class TestSandboxResponseContract:
    """Tests for response classification."""

    @pytest.mark.asyncio
    async def test_success_true_returns_completed(
        self,
        mocker: MockerFixture,
    ) -> None:
        """HTTP 200 + success=true → completed."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://sandbox:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "output": {"summary": "all good", "actions": []},
        }
        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient"
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response)
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
                "sandbox_image": "s:l",
                "context": {},
            },
            spawner=mock_spawner,
        )

        assert result["status"] == "completed"
        assert result["output"]["summary"] == "all good"

    @pytest.mark.asyncio
    async def test_success_false_returns_failed(
        self,
        mocker: MockerFixture,
    ) -> None:
        """HTTP 200 + success=false → failed."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://sandbox:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "error": "LLM returned empty response",
        }
        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient"
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response)
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
                "sandbox_image": "s:l",
                "context": {},
            },
            spawner=mock_spawner,
        )

        assert result["status"] == "failed"
        assert "empty response" in result["error"]

    @pytest.mark.asyncio
    async def test_http_502_raises_for_retry(
        self,
        mocker: MockerFixture,
    ) -> None:
        """HTTP 502 → RuntimeError (Temporal retries)."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://sandbox:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 502
        mock_http = mocker.patch(
            "agents.workflow.temporal_activities.httpx.AsyncClient"
        )
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                post=mocker.AsyncMock(return_value=mock_response)
            ),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="502"):
            await run_sandbox_step(
                {
                    "step": {"name": "s1", "prompt": "check", "output_key": "r1"},
                    "workflow_id": "wf-1",
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4",
                        "credentials_secret": "k",
                    },
                    "sandbox_image": "s:l",
                    "context": {},
                },
                spawner=mock_spawner,
            )
