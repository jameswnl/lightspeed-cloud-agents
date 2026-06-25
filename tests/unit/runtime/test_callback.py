"""Unit tests for agent runtime result callback (Phase 8 Task 4)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.runtime.callback import ResultCallback, get_callback


class TestResultCallback:
    """Tests for ResultCallback."""

    @pytest.mark.asyncio
    async def test_callback_posts_result_on_completion(self) -> None:
        """Successful callback returns True."""
        callback = ResultCallback(
            callback_url="http://runner:8080/v1/workflows/wf-1/steps/r1/result",
            attempt=1,
            max_retries=1,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("agents.runtime.callback.httpx.AsyncClient") as mock_client:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await callback.post_result(
                status="completed",
                output={"summary": "done"},
            )

        assert result is True
        mock_ctx.post.assert_called_once()
        call_kwargs = mock_ctx.post.call_args
        assert call_kwargs[1]["json"]["status"] == "completed"
        assert call_kwargs[1]["json"]["attempt"] == 1

    @pytest.mark.asyncio
    async def test_callback_posts_error_on_failure(self) -> None:
        """Failed step callback includes error."""
        callback = ResultCallback(
            callback_url="http://runner:8080/ingest",
            attempt=1,
            max_retries=1,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("agents.runtime.callback.httpx.AsyncClient") as mock_client:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await callback.post_result(
                status="failed",
                error="LLM timeout",
            )

        assert result is True
        call_kwargs = mock_ctx.post.call_args
        assert call_kwargs[1]["json"]["error"] == "LLM timeout"

    @pytest.mark.asyncio
    async def test_callback_includes_auth_header(self) -> None:
        """Bearer token is included in callback headers."""
        callback = ResultCallback(
            callback_url="http://runner:8080/ingest",
            auth_token="secret-token",
            attempt=1,
            max_retries=1,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("agents.runtime.callback.httpx.AsyncClient") as mock_client:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await callback.post_result(status="completed", output={})

        call_kwargs = mock_ctx.post.call_args
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer secret-token"

    @pytest.mark.asyncio
    async def test_callback_retries_on_transient_failure(self) -> None:
        """Callback retries on exception."""
        callback = ResultCallback(
            callback_url="http://runner:8080/ingest",
            attempt=1,
            max_retries=2,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200

        call_count = 0

        async def failing_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Connection refused")
            return mock_response

        with patch("agents.runtime.callback.httpx.AsyncClient") as mock_client, \
             patch("agents.runtime.callback.asyncio.sleep", new_callable=AsyncMock):
            mock_ctx = AsyncMock()
            mock_ctx.post = failing_then_success
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await callback.post_result(status="completed", output={})

        assert result is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_callback_failure_returns_false(self) -> None:
        """All retries exhausted returns False (no crash)."""
        callback = ResultCallback(
            callback_url="http://runner:8080/ingest",
            attempt=1,
            max_retries=2,
        )

        with patch("agents.runtime.callback.httpx.AsyncClient") as mock_client, \
             patch("agents.runtime.callback.asyncio.sleep", new_callable=AsyncMock):
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(side_effect=Exception("always fails"))
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await callback.post_result(status="completed", output={})

        assert result is False

    @pytest.mark.asyncio
    async def test_callback_409_treated_as_success(self) -> None:
        """409 (duplicate/stale) is treated as success."""
        callback = ResultCallback(
            callback_url="http://runner:8080/ingest",
            attempt=1,
            max_retries=1,
        )

        mock_response = MagicMock()
        mock_response.status_code = 409

        with patch("agents.runtime.callback.httpx.AsyncClient") as mock_client:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await callback.post_result(status="completed", output={})

        assert result is True


class TestGetCallback:
    """Tests for get_callback() factory."""

    def test_no_callback_when_env_not_set(self) -> None:
        """Returns None when RESULT_CALLBACK_URL is not set."""
        import os
        with patch.dict(os.environ, {}, clear=True):
            assert get_callback() is None

    def test_callback_created_from_env(self) -> None:
        """Creates callback from env vars."""
        import os
        with patch.dict(os.environ, {
            "RESULT_CALLBACK_URL": "http://runner:8080/ingest",
            "AGENT_API_TOKEN": "tok-123",
            "RESULT_CALLBACK_ATTEMPT": "2",
        }):
            cb = get_callback()
            assert cb is not None
            assert cb._callback_url == "http://runner:8080/ingest"
            assert cb._auth_token == "tok-123"
            assert cb._attempt == 2
