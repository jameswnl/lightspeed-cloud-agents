"""Per-user token bucket rate limiter and ASGI middleware.

Provides in-memory per-caller rate limiting using the token bucket
algorithm. Keys are derived from bearer tokens (sha256 hash prefix)
or client IP. Health and metrics endpoints are exempt.

Per-process only -- no cross-replica state sharing. Sufficient for
single-replica deployments; Redis-backed storage is a future
enhancement for multi-replica.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field

from starlette.responses import JSONResponse

from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.temporal_metrics import ls_rate_limit_rejections_total

logger = logging.getLogger(__name__)


@dataclass
class _BucketState:
    """Internal state for a single key's token bucket."""

    tokens: float
    last_refill: float


class TokenBucket:
    """Per-key token bucket rate limiter.

    Each key gets its own bucket with a configurable refill rate and
    burst capacity. Thread-safe under asyncio (no concurrent mutation
    of shared state between coroutines without the GIL).

    Attributes:
        rate: Tokens added per second. 0 means unlimited.
        burst: Maximum token capacity (burst allowance).
    """

    def __init__(self, rate: float, burst: int) -> None:
        """Initialize the rate limiter.

        Parameters:
            rate: Tokens per second (e.g., 10.0 = 10 req/s). 0 means
                unlimited (all requests allowed).
            burst: Maximum tokens (burst capacity).
        """
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, _BucketState] = {}
        self._call_count = 0
        self._cleanup_interval = 100

    def allow(self, key: str) -> bool:
        """Check if a request is allowed for the given key.

        Refills tokens based on elapsed time, then tries to consume one.

        Parameters:
            key: Caller identifier (hashed token or IP).

        Returns:
            True if the request is allowed, False if rate limited.
        """
        if self.rate <= 0:
            return True

        now = time.monotonic()
        state = self._buckets.get(key)

        if state is None:
            state = _BucketState(tokens=float(self.burst), last_refill=now)
            self._buckets[key] = state

        # Refill tokens based on elapsed time
        elapsed = now - state.last_refill
        if elapsed > 0:
            state.tokens = min(
                float(self.burst),
                state.tokens + elapsed * self.rate,
            )
            state.last_refill = now

        # Try to consume a token
        if state.tokens >= 1.0:
            state.tokens -= 1.0
            self._maybe_cleanup(now)
            return True

        self._maybe_cleanup(now)
        return False

    def _maybe_cleanup(self, now: float) -> None:
        """Conditionally prune stale keys to bound memory usage.

        Called on every Nth allow() call. Removes keys that haven't
        been seen in 2x the refill period.

        Parameters:
            now: Current monotonic time.
        """
        self._call_count += 1
        if self._call_count < self._cleanup_interval:
            return
        self._call_count = 0
        self._cleanup_stale(now)

    def _cleanup_stale(self, now: float) -> None:
        """Prune keys that haven't been seen in 2x the refill period.

        Parameters:
            now: Current monotonic time.
        """
        if self.rate <= 0:
            return
        max_age = 2.0 * self.burst / self.rate
        stale_keys = [
            k for k, s in self._buckets.items() if now - s.last_refill > max_age
        ]
        for k in stale_keys:
            del self._buckets[k]


class RateLimitMiddleware:
    """ASGI middleware for per-caller rate limiting.

    Enforces per-caller rate limits using a token bucket algorithm.
    The rate limit key is derived from the Authorization header
    (sha256 hash prefix of the bearer token) or the client IP when
    no auth header is present.

    Health, liveness, readiness, and metrics endpoints are exempt.

    Attributes:
        app: The wrapped ASGI application.
        limiter: The token bucket rate limiter instance.
    """

    EXEMPT_PATHS = {"/healthz", "/livez", "/readyz", "/metrics"}

    # Throttle audit events: at most 1 per key per this many seconds.
    _AUDIT_THROTTLE_SECONDS = 60.0

    def __init__(self, app: object, rate: float, burst: int) -> None:
        """Initialize the middleware.

        Parameters:
            app: The ASGI application to wrap.
            rate: Tokens per second per caller. 0 disables rate limiting.
            burst: Burst capacity per caller.
        """
        self.app = app
        self.limiter = TokenBucket(rate=rate, burst=burst)
        self._audit_last_emitted: dict[str, float] = {}

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        """Process an ASGI request, enforcing rate limits.

        For HTTP requests to non-exempt paths, checks the token bucket
        for the caller's key. Returns 429 with Retry-After header when
        the rate limit is exceeded.

        Non-HTTP scopes (WebSocket, lifespan) pass through unchanged.

        Parameters:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        key = self._extract_key(scope)
        allowed = self.limiter.allow(key)

        # Piggyback audit dict cleanup on bucket cleanup cycle
        if self.limiter._call_count == 0 and self._audit_last_emitted:
            self._prune_stale_audit()

        if not allowed:
            ls_rate_limit_rejections_total.labels(path=path).inc()
            self._maybe_emit_audit(key, path, scope)
            retry_after = str(max(1, math.ceil(1 / self.limiter.rate))) if self.limiter.rate > 0 else "60"
            response = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": retry_after},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _prune_stale_audit(self) -> None:
        """Remove audit entries whose rate-limit buckets have been cleaned up."""
        stale = [k for k in self._audit_last_emitted if k not in self.limiter._buckets]
        for k in stale:
            del self._audit_last_emitted[k]

    def _extract_key(self, scope: dict) -> str:
        """Extract a rate limit key from the ASGI scope.

        Uses sha256(bearer_token)[:16] when an Authorization header is
        present, client IP when available, or 'anonymous' as fallback.

        Parameters:
            scope: ASGI connection scope.

        Returns:
            A string key identifying the caller for rate limiting.
        """
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"")
        if auth_header.startswith(b"Bearer "):
            token = auth_header[7:]
            return hashlib.sha256(token).hexdigest()[:16]

        client = scope.get("client")
        if client:
            return f"ip:{client[0]}"

        return "anonymous"

    def _maybe_emit_audit(self, key: str, path: str, scope: dict) -> None:
        """Emit an audit event for rate limit rejection, throttled per key.

        At most one audit event per key per _AUDIT_THROTTLE_SECONDS to
        prevent audit log flooding from a rate-limited caller.

        Parameters:
            key: The anonymized caller key.
            path: The request path.
            scope: ASGI connection scope (for client IP extraction).
        """
        now = time.monotonic()
        last = self._audit_last_emitted.get(key)
        if last is not None and (now - last) < self._AUDIT_THROTTLE_SECONDS:
            return

        self._audit_last_emitted[key] = now
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        emit_audit(
            event_type="rate_limit_exceeded",
            details={
                "caller_key": key,
                "path": path,
                "client_ip": client_ip,
            },
        )
