"""Unit tests for per-user token bucket rate limiter and ASGI middleware (TDD)."""

from __future__ import annotations

import hashlib
import time
from unittest.mock import patch

import pytest
from pytest_mock import MockerFixture


# ---------------------------------------------------------------------------
# TokenBucket tests
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Tests for the TokenBucket rate limiter."""

    def test_first_request_allowed(self) -> None:
        """First request is allowed (bucket starts full)."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, burst=5)
        assert bucket.allow("user-1") is True

    def test_burst_requests_all_allowed(self) -> None:
        """Up to burst-count requests are allowed without waiting."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, burst=5)
        results = [bucket.allow("user-1") for _ in range(5)]
        assert all(results)

    def test_request_after_burst_exhaustion_rejected(self) -> None:
        """Request after burst exhaustion is rejected."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, burst=3)
        for _ in range(3):
            bucket.allow("user-1")
        assert bucket.allow("user-1") is False

    def test_tokens_refill_after_waiting(self) -> None:
        """After waiting, tokens refill and request is allowed."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=10.0, burst=1)
        bucket.allow("user-1")  # consume the 1 token
        assert bucket.allow("user-1") is False

        # Simulate time passing (0.2s at rate=10 => 2 tokens refilled)
        with patch("cloud_agents.workflow.rate_limiter.time") as mock_time:
            # First call was at some time T; set monotonic to T + 0.2
            original_time = time.monotonic()
            mock_time.monotonic.return_value = original_time + 0.2
            # Need to re-create to use mock properly; instead, manipulate internal state
        # Simpler approach: manipulate the bucket's internal state
        bucket._buckets["user-1"].last_refill -= 0.2
        assert bucket.allow("user-1") is True

    def test_different_keys_independent(self) -> None:
        """Different keys have independent rate limits."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, burst=1)
        bucket.allow("user-1")  # exhaust user-1
        assert bucket.allow("user-1") is False
        assert bucket.allow("user-2") is True  # user-2 unaffected

    def test_stale_keys_cleaned_up(self) -> None:
        """Stale keys are cleaned up after cleanup interval."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, burst=1)
        bucket.allow("old-user")
        # Age the entry so it's considered stale
        stale_age = 2 * (1 / 1.0) + 1  # 2 * burst/rate + margin
        bucket._buckets["old-user"].last_refill -= stale_age
        bucket._call_count = bucket._cleanup_interval - 1  # trigger cleanup on next call
        bucket.allow("new-user")  # triggers cleanup
        assert "old-user" not in bucket._buckets

    def test_rate_zero_means_unlimited(self) -> None:
        """rate=0 means unlimited -- all requests pass."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=0, burst=0)
        results = [bucket.allow("user-1") for _ in range(100)]
        assert all(results)

    def test_burst_zero_with_positive_rate_rejects_all(self) -> None:
        """burst=0 with positive rate rejects all requests."""
        from cloud_agents.workflow.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=1.0, burst=0)
        assert bucket.allow("user-1") is False


# ---------------------------------------------------------------------------
# RateLimitMiddleware tests
# ---------------------------------------------------------------------------


class TestRateLimitMiddleware:
    """Tests for the RateLimitMiddleware ASGI middleware."""

    @pytest.mark.asyncio
    async def test_requests_below_limit_pass_through(self) -> None:
        """Requests below rate limit pass through to the app."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        app_called = False

        async def inner_app(scope, receive, send):
            nonlocal app_called
            app_called = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=10.0, burst=20)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }
        responses: list[dict] = []

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def capture_send(message):
            responses.append(message)

        await middleware(scope, mock_receive, capture_send)
        assert app_called

    @pytest.mark.asyncio
    async def test_requests_above_limit_return_429(self) -> None:
        """Requests above rate limit return 429."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=2)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }
        responses: list[dict] = []

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def capture_send(message):
            responses.append(message)

        # Exhaust the burst
        for _ in range(2):
            await middleware(scope, mock_receive, capture_send)
        responses.clear()

        # This should be rate limited
        await middleware(scope, mock_receive, capture_send)
        status = next(r["status"] for r in responses if r["type"] == "http.response.start")
        assert status == 429

    @pytest.mark.asyncio
    async def test_429_includes_retry_after_header(self) -> None:
        """429 response includes Retry-After header."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=2.0, burst=1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }
        responses: list[dict] = []

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def capture_send(message):
            responses.append(message)

        await middleware(scope, mock_receive, capture_send)  # exhaust
        responses.clear()
        await middleware(scope, mock_receive, capture_send)  # rate limited

        start_msg = next(r for r in responses if r["type"] == "http.response.start")
        headers = dict(start_msg.get("headers", []))
        assert b"retry-after" in headers

    @pytest.mark.asyncio
    async def test_health_endpoints_exempt(self) -> None:
        """Health, liveness, readiness, and metrics endpoints are exempt."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        call_count = 0

        async def inner_app(scope, receive, send):
            nonlocal call_count
            call_count += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        # rate=1, burst=1 means only 1 request allowed
        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=1)

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        for path in ["/healthz", "/livez", "/readyz", "/metrics"]:
            scope = {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [],
            }
            await middleware(scope, mock_receive, noop_send)

        # All 4 health paths should have passed through
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_different_bearer_tokens_independent(self) -> None:
        """Different bearer tokens get independent rate limits."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=1)

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        responses: list[dict] = []

        async def capture_send(message):
            responses.append(message)

        # Exhaust token-A
        scope_a = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [(b"authorization", b"Bearer token-A")],
        }
        await middleware(scope_a, mock_receive, capture_send)
        responses.clear()

        # token-A should be limited
        await middleware(scope_a, mock_receive, capture_send)
        status_a = next(r["status"] for r in responses if r["type"] == "http.response.start")
        assert status_a == 429
        responses.clear()

        # token-B should still work (independent)
        scope_b = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [(b"authorization", b"Bearer token-B")],
        }
        await middleware(scope_b, mock_receive, capture_send)
        status_b = next(r["status"] for r in responses if r["type"] == "http.response.start")
        assert status_b == 200

    @pytest.mark.asyncio
    async def test_rate_zero_disables_limiting(self) -> None:
        """rate=0 disables rate limiting -- all requests pass."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        app_called = 0

        async def inner_app(scope, receive, send):
            nonlocal app_called
            app_called += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=0, burst=0)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        for _ in range(50):
            await middleware(scope, mock_receive, noop_send)
        assert app_called == 50

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self) -> None:
        """Non-HTTP scopes (e.g., WebSocket) pass through without rate limiting."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        app_called = False

        async def inner_app(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=1)
        await middleware({"type": "websocket"}, None, None)
        assert app_called

    @pytest.mark.asyncio
    async def test_key_from_bearer_token_is_hashed(self) -> None:
        """Key extracted from bearer token is sha256-hashed, not raw token."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=10.0, burst=20)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [(b"authorization", b"Bearer my-secret-token")],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        await middleware(scope, mock_receive, noop_send)

        # The bucket key should be the hash prefix, not the raw token
        expected_key = hashlib.sha256(b"my-secret-token").hexdigest()[:16]
        assert expected_key in middleware.limiter._buckets
        assert "my-secret-token" not in middleware.limiter._buckets

    @pytest.mark.asyncio
    async def test_key_fallback_to_client_ip(self) -> None:
        """Falls back to client IP when no Authorization header."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=10.0, burst=20)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
            "client": ("192.168.1.1", 12345),
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        await middleware(scope, mock_receive, noop_send)
        assert "ip:192.168.1.1" in middleware.limiter._buckets

    @pytest.mark.asyncio
    async def test_anonymous_key_when_no_auth_no_client(self) -> None:
        """Falls back to 'anonymous' when no auth header and no client IP."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=10.0, burst=20)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        await middleware(scope, mock_receive, noop_send)
        assert "anonymous" in middleware.limiter._buckets


# ---------------------------------------------------------------------------
# Prometheus metrics tests
# ---------------------------------------------------------------------------


class TestRateLimitMetrics:
    """Tests for rate limit Prometheus metrics."""

    @pytest.mark.asyncio
    async def test_counter_incremented_on_rejection(self, mocker: MockerFixture) -> None:
        """ls_rate_limit_rejections_total is incremented when a request is rate limited."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware
        from cloud_agents.workflow.temporal_metrics import ls_rate_limit_rejections_total

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        # Get the counter value before
        before = ls_rate_limit_rejections_total.labels(path="/v1/workflows/run")._value.get()

        await middleware(scope, mock_receive, noop_send)  # allowed
        await middleware(scope, mock_receive, noop_send)  # rejected

        after = ls_rate_limit_rejections_total.labels(path="/v1/workflows/run")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_counter_not_incremented_on_pass(self, mocker: MockerFixture) -> None:
        """Counter is not incremented when a request passes."""
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware
        from cloud_agents.workflow.temporal_metrics import ls_rate_limit_rejections_total

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=10.0, burst=20)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        before = ls_rate_limit_rejections_total.labels(path="/v1/workflows/run")._value.get()
        await middleware(scope, mock_receive, noop_send)
        after = ls_rate_limit_rejections_total.labels(path="/v1/workflows/run")._value.get()
        assert after == before


# ---------------------------------------------------------------------------
# Audit event tests
# ---------------------------------------------------------------------------


class TestRateLimitAudit:
    """Tests for rate limit audit event emission."""

    @pytest.mark.asyncio
    async def test_audit_emitted_on_first_rejection(self, mocker: MockerFixture) -> None:
        """Audit event emitted on first rate limit rejection."""
        mock_emit = mocker.patch("cloud_agents.workflow.rate_limiter.emit_audit")

        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        await middleware(scope, mock_receive, noop_send)  # allowed
        await middleware(scope, mock_receive, noop_send)  # rejected

        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args
        assert call_kwargs[1]["event_type"] == "rate_limit_exceeded"

    @pytest.mark.asyncio
    async def test_audit_throttled_for_same_key(self, mocker: MockerFixture) -> None:
        """Subsequent rapid rejections for the same key do not emit duplicate audit events."""
        mock_emit = mocker.patch("cloud_agents.workflow.rate_limiter.emit_audit")

        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RateLimitMiddleware(inner_app, rate=1.0, burst=1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],
        }

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def noop_send(message):
            pass

        await middleware(scope, mock_receive, noop_send)  # allowed
        # Multiple rapid rejections
        for _ in range(5):
            await middleware(scope, mock_receive, noop_send)  # rejected

        # Should only emit once (throttled to 1 per key per minute)
        assert mock_emit.call_count == 1


# ---------------------------------------------------------------------------
# Entrypoint wiring tests
# ---------------------------------------------------------------------------


class TestRateLimitEntrypointWiring:
    """Tests for rate limit middleware wiring in build_temporal_app."""

    def test_middleware_not_added_when_disabled(self, mocker: MockerFixture) -> None:
        """RateLimitMiddleware not in middleware stack when RATE_LIMIT_ENABLED=false."""
        mocker.patch.dict("os.environ", {"RATE_LIMIT_ENABLED": "false"}, clear=False)
        from cloud_agents.workflow.temporal_entrypoint import build_temporal_app

        app = build_temporal_app(temporal_url="localhost:7233")
        middleware_types = [type(m.cls).__name__ if hasattr(m, "cls") else type(m).__name__
                           for m in getattr(app, "user_middleware", [])]
        assert "RateLimitMiddleware" not in str(middleware_types)

    def test_middleware_added_when_enabled(self, mocker: MockerFixture) -> None:
        """RateLimitMiddleware is in middleware stack when RATE_LIMIT_ENABLED=true."""
        mocker.patch.dict(
            "os.environ",
            {
                "RATE_LIMIT_ENABLED": "true",
                "RATE_LIMIT_RATE": "5",
                "RATE_LIMIT_BURST": "10",
            },
            clear=False,
        )
        from cloud_agents.workflow.temporal_entrypoint import build_temporal_app

        app = build_temporal_app(temporal_url="localhost:7233")
        # Check that at least one middleware in the stack is our RateLimitMiddleware
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        found = any(
            m.cls is RateLimitMiddleware
            for m in getattr(app, "user_middleware", [])
            if hasattr(m, "cls")
        )
        assert found, "RateLimitMiddleware not found in app middleware stack"

    def test_custom_rate_and_burst_from_env(self, mocker: MockerFixture) -> None:
        """Custom rate and burst values read from environment variables."""
        mocker.patch.dict(
            "os.environ",
            {
                "RATE_LIMIT_ENABLED": "true",
                "RATE_LIMIT_RATE": "25",
                "RATE_LIMIT_BURST": "50",
            },
            clear=False,
        )
        from cloud_agents.workflow.temporal_entrypoint import build_temporal_app

        app = build_temporal_app(temporal_url="localhost:7233")
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        # Find the middleware kwargs
        for m in getattr(app, "user_middleware", []):
            if hasattr(m, "cls") and m.cls is RateLimitMiddleware:
                assert m.kwargs.get("rate") == 25.0
                assert m.kwargs.get("burst") == 50
                return
        pytest.fail("RateLimitMiddleware not found in middleware stack")
