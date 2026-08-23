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


class TestHttpRequestUrlValidation:
    """Tests for SSRF URL validation."""

    def test_blocks_metadata_ip(self) -> None:
        """Blocks AWS/GCP metadata endpoint."""
        from cloud_agents.tools.http_request import http_request

        result = http_request("http://169.254.169.254/latest/meta-data/")
        assert "blocked" in result.lower()

    def test_blocks_private_ip(self) -> None:
        """Blocks RFC 1918 private addresses."""
        from cloud_agents.tools.http_request import http_request

        result = http_request("http://10.0.0.1/internal")
        assert "blocked" in result.lower()

    def test_blocks_localhost(self) -> None:
        """Blocks localhost."""
        from cloud_agents.tools.http_request import http_request

        result = http_request("http://127.0.0.1/secret")
        assert "blocked" in result.lower()

    def test_blocks_file_scheme(self) -> None:
        """Blocks file:// URLs."""
        from cloud_agents.tools.http_request import http_request

        result = http_request("file:///etc/passwd")
        assert "not allowed" in result.lower()

    def test_allows_public_https(self) -> None:
        """Does not block public HTTPS URLs (validation passes)."""
        from cloud_agents.tools.http_request import _validate_url

        assert _validate_url("https://api.example.com/data") is None

    def test_rejects_unsupported_method(self) -> None:
        """Rejects DELETE and other unsupported methods."""
        from cloud_agents.tools.http_request import http_request

        result = http_request("https://example.com", method="DELETE")
        assert "not supported" in result.lower()


class TestHttpRequest:
    """Tests for http_request tool function behavior."""

    def _mock_streaming_client(self, response_bytes: bytes) -> MagicMock:
        """Create a mock httpx.Client with streaming response."""
        mock_stream_response = MagicMock()
        mock_stream_response.raise_for_status = MagicMock()
        mock_stream_response.iter_bytes = MagicMock(return_value=[response_bytes])
        mock_stream_response.__enter__ = MagicMock(return_value=mock_stream_response)
        mock_stream_response.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream = MagicMock(return_value=mock_stream_response)

        return mock_client

    def test_get_request_returns_body(self) -> None:
        """http_request GET returns response body."""
        from cloud_agents.tools.http_request import http_request

        mock_client = self._mock_streaming_client(b'{"status": "ok"}')

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_cls:
            mock_cls.return_value = mock_client

            result = http_request("https://example.com/api")

        assert result == '{"status": "ok"}'

    def test_post_request_with_body(self) -> None:
        """http_request POST sends body."""
        from cloud_agents.tools.http_request import http_request

        mock_client = self._mock_streaming_client(b'{"created": true}')

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_cls:
            mock_cls.return_value = mock_client

            result = http_request(
                "https://example.com/api",
                method="POST",
                body='{"key": "value"}',
            )

        assert result == '{"created": true}'
        mock_client.stream.assert_called_once_with(
            "POST", "https://example.com/api", content='{"key": "value"}', headers=None
        )

    def test_size_limit_streaming(self) -> None:
        """http_request returns error when streamed response exceeds size limit."""
        from cloud_agents.tools.http_request import http_request

        large_chunk = b"x" * 1_048_577
        mock_client = self._mock_streaming_client(large_chunk)

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_cls:
            mock_cls.return_value = mock_client

            result = http_request("https://example.com/large")

        assert "too large" in result.lower()

    def test_timeout_enforcement(self) -> None:
        """http_request returns error string on timeout."""
        from cloud_agents.tools.http_request import http_request

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream.side_effect = httpx.TimeoutException("timed out")
            mock_cls.return_value = mock_client

            result = http_request("https://example.com/slow", timeout_seconds=5)

        assert "timed out" in result.lower()

    def test_timeout_clamped(self) -> None:
        """Timeout is clamped to max 300 seconds."""
        from cloud_agents.tools.http_request import http_request

        mock_client = self._mock_streaming_client(b"ok")

        with patch("cloud_agents.tools.http_request.httpx.Client") as mock_cls:
            mock_cls.return_value = mock_client

            http_request("https://example.com", timeout_seconds=86400)

        mock_cls.assert_called_once_with(timeout=300)
