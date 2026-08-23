"""Tests for the http_request built-in tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx


class TestHttpRequestRegistration:
    """Tests that http_request is properly registered."""

    def test_registered_after_import(self) -> None:
        """Importing http_request module registers 'http_request' tool."""
        from cloud_agents.workflow.executor.step.tools import list_tools

        import cloud_agents.tools.http_request  # noqa: F401

        assert "http_request" in list_tools()


class TestHttpRequest:
    """Tests for http_request tool function behavior."""

    def test_get_request_returns_body(self) -> None:
        """http_request GET returns response body."""
        from cloud_agents.tools.http_request import http_request

        mock_response = MagicMock()
        mock_response.text = '{"status": "ok"}'
        mock_response.content = b'{"status": "ok"}'

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = http_request("https://example.com/api")

        assert result == '{"status": "ok"}'
        mock_client.get.assert_called_once()

    def test_post_request_with_body(self) -> None:
        """http_request POST sends body and returns response."""
        from cloud_agents.tools.http_request import http_request

        mock_response = MagicMock()
        mock_response.text = '{"created": true}'
        mock_response.content = b'{"created": true}'

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = http_request(
                "https://example.com/api",
                method="POST",
                body='{"key": "value"}',
            )

        assert result == '{"created": true}'
        mock_client.post.assert_called_once_with(
            "https://example.com/api",
            content='{"key": "value"}',
            headers=None,
        )

    def test_timeout_enforcement(self) -> None:
        """http_request returns error string on timeout."""
        from cloud_agents.tools.http_request import http_request

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.TimeoutException("timed out")
            mock_client_cls.return_value = mock_client

            result = http_request("https://example.com/slow", timeout_seconds=5)

        assert "timed out" in result.lower()
        assert "5s" in result

    def test_size_limit_enforcement(self) -> None:
        """http_request returns error when response exceeds size limit."""
        from cloud_agents.tools.http_request import http_request

        mock_response = MagicMock()
        mock_response.content = b"x" * (1_048_577)  # Just over 1 MB
        mock_response.text = "x" * (1_048_577)

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = http_request("https://example.com/large")

        assert "too large" in result.lower()

    def test_http_error_returns_message(self) -> None:
        """http_request returns error string on HTTP error status."""
        from cloud_agents.tools.http_request import http_request

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "not found",
                request=MagicMock(),
                response=mock_response,
            )
            mock_client_cls.return_value = mock_client

            result = http_request("https://example.com/missing")

        assert "404" in result

    def test_custom_headers(self) -> None:
        """http_request passes custom headers."""
        from cloud_agents.tools.http_request import http_request

        mock_response = MagicMock()
        mock_response.text = "ok"
        mock_response.content = b"ok"

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            http_request(
                "https://example.com/api",
                headers={"Authorization": "Bearer token"},
            )

        mock_client.get.assert_called_once_with(
            "https://example.com/api",
            headers={"Authorization": "Bearer token"},
        )
