"""Authentication middleware for agent and workflow endpoints.

Validates bearer tokens on protected endpoints. Health and liveness
probes are exempt. Tokens are configured via AGENT_API_TOKENS (comma-
separated, preferred) or AGENT_API_TOKEN (single, backward compat).

Supports optional token expiry via timestamp suffix: ``token:unix_ts``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.authorization import CallerIdentity

logger = logging.getLogger(__name__)

EXEMPT_PATHS = {"/healthz", "/livez", "/metrics"}


def _parse_token_map(raw_tokens: list[str]) -> dict[str, float | None]:
    """Parse raw tokens into a mapping of token value to optional expiry.

    Tokens may include an expiry timestamp suffix separated by ``:``.
    For example ``mytoken:1735689600`` means the token ``mytoken`` expires
    at Unix timestamp 1735689600. If the part after the last ``:`` is not
    a valid number, the entire raw string is treated as the token with no
    expiry.

    Parameters:
        raw_tokens: List of raw token strings, possibly with ``:timestamp``.

    Returns:
        Dict mapping token value to expiry timestamp (or None if no expiry).
    """
    token_map: dict[str, float | None] = {}
    for raw in raw_tokens:
        if not raw:
            continue
        if ":" in raw:
            prefix, suffix = raw.rsplit(":", 1)
            try:
                expiry = float(suffix)
                token_map[prefix] = expiry
            except ValueError:
                # Suffix is not a number — treat entire raw string as token
                token_map[raw] = None
        else:
            token_map[raw] = None
    return token_map


def _token_prefix(token: str) -> str:
    """Return the first 4 characters of a token for safe logging.

    Parameters:
        token: The full token string.

    Returns:
        First 4 characters (or fewer if the token is shorter).
    """
    return token[:4]


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Middleware that validates Bearer tokens on non-exempt endpoints.

    Supports multiple tokens for zero-downtime rotation and optional
    per-token expiry via timestamp suffix.

    Attributes:
        valid_tokens: Mapping of token value to optional expiry timestamp.
    """

    def __init__(self, app: object, tokens: list[str] | None = None) -> None:
        """Initialize with one or more valid tokens.

        Args:
            app: The ASGI application.
            tokens: List of valid token strings (may include ``:timestamp``
                suffix for expiry). Empty or None disables auth.
        """
        super().__init__(app)
        self.valid_tokens: dict[str, float | None] = _parse_token_map(tokens or [])

    async def dispatch(self, request: Request, call_next: object) -> object:
        """Check authorization on non-exempt paths."""
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        if not self.valid_tokens:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Missing or malformed Authorization header on %s", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing authorization token"},
            )

        presented_token = auth_header[7:]

        if presented_token not in self.valid_tokens:
            logger.warning(
                "Rejected bearer token (length=%d, prefix=%s...)",
                len(presented_token),
                _token_prefix(presented_token),
            )
            emit_audit(
                event_type="auth_rejected",
                details={
                    "token_prefix": _token_prefix(presented_token),
                    "path": request.url.path,
                    "reason": "invalid",
                },
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing authorization token"},
            )

        expiry = self.valid_tokens[presented_token]
        if expiry is not None and time.time() > expiry:
            logger.warning(
                "Rejected expired bearer token (length=%d, prefix=%s...)",
                len(presented_token),
                _token_prefix(presented_token),
            )
            emit_audit(
                event_type="auth_rejected",
                details={
                    "token_prefix": _token_prefix(presented_token),
                    "path": request.url.path,
                    "reason": "expired",
                },
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing authorization token"},
            )

        request.state.caller_identity = CallerIdentity(
            username="anonymous", auth_mode="shared_secret"
        )
        return await call_next(request)


class TokenReviewAuthMiddleware(BaseHTTPMiddleware):
    """Validates bearer tokens via K8s TokenReview API.

    Each spawned Job gets a projected ServiceAccount token with
    audience scoping to 'cloud-agents'. This middleware validates
    incoming tokens against the K8s API server.

    Attributes:
        audience: Expected token audience.
    """

    AUDIENCE = "cloud-agents"

    def __init__(self, app: object) -> None:
        """Initialize the TokenReview middleware.

        Args:
            app: The ASGI application.
        """
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: object) -> object:
        """Validate bearer token via K8s TokenReview API."""
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing bearer token"},
            )

        token = auth_header[7:]
        review_result = await self._validate_token(token)
        if review_result is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Token validation failed"},
            )

        request.state.caller_identity = CallerIdentity(
            username=review_result.status.user.username,
            uid=review_result.status.user.uid,
            groups=review_result.status.user.groups or [],
            auth_mode="sa_token",
        )
        return await call_next(request)

    async def _validate_token(self, token: str) -> object | None:
        """Call K8s TokenReview API to validate the token.

        Returns:
            The TokenReview result on success, None on failure.
        """
        try:
            from kubernetes import client, config

            config.load_incluster_config()
            auth_api = client.AuthenticationV1Api()
            review = client.V1TokenReview(
                spec=client.V1TokenReviewSpec(
                    token=token,
                    audiences=[self.AUDIENCE],
                ),
            )
            result = auth_api.create_token_review(review)
            if result.status.authenticated:
                return result
            return None
        except Exception:
            return None


def get_auth_mode() -> str:
    """Get the authentication mode from environment.

    Returns:
        'shared_secret' (default) or 'sa_token'.
    """
    return os.environ.get("AUTH_MODE", "shared_secret")


def get_api_token() -> str:
    """Get the API token from environment.

    Both Podman and K8s use AGENT_API_TOKEN -- injected via env var
    (Podman) or K8s Secret secretKeyRef (K8s). The same shared
    token is used by all pods in the deployment.

    Returns:
        Token string. Empty string means auth is disabled.
    """
    return os.environ.get("AGENT_API_TOKEN", "")


def get_api_tokens() -> list[str]:
    """Get all valid API tokens from environment.

    Reads ``AGENT_API_TOKENS`` (comma-separated, preferred) and
    ``AGENT_API_TOKEN`` (single, backward compat). Both are merged;
    duplicates are removed while preserving order.

    Returns:
        List of token strings (may include ``:timestamp`` suffix).
        Empty list means auth is disabled.
    """
    multi = os.environ.get("AGENT_API_TOKENS", "")
    single = os.environ.get("AGENT_API_TOKEN", "")
    tokens = [t.strip() for t in multi.split(",") if t.strip()]
    if single and single not in tokens:
        tokens.append(single)
    return tokens


def create_bearer_auth_dependency(
    tokens: list[str],
):
    """Create a FastAPI dependency function that validates bearer tokens.

    Returns an async callable suitable for ``Depends()``. Validates the
    presented bearer token against the provided token list, supporting
    multi-token rotation and optional expiry via timestamp suffix.

    Parameters:
        tokens: List of valid token strings (may include ``:timestamp``).

    Returns:
        Async dependency function for FastAPI router dependencies.
    """
    token_map = _parse_token_map(tokens)

    async def verify_bearer(request: Request) -> None:
        """Validate bearer token from Authorization header.

        Parameters:
            request: The incoming FastAPI request.

        Raises:
            HTTPException: 401 when token is missing, invalid, or expired.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing authorization token",
            )

        presented_token = auth_header[7:]

        if presented_token not in token_map:
            logger.warning(
                "Rejected bearer token (length=%d, prefix=%s...)",
                len(presented_token),
                _token_prefix(presented_token),
            )
            emit_audit(
                event_type="auth_rejected",
                details={
                    "token_prefix": _token_prefix(presented_token),
                    "path": request.url.path,
                    "reason": "invalid",
                },
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing authorization token",
            )

        expiry = token_map[presented_token]
        if expiry is not None and time.time() > expiry:
            logger.warning(
                "Rejected expired bearer token (length=%d, prefix=%s...)",
                len(presented_token),
                _token_prefix(presented_token),
            )
            emit_audit(
                event_type="auth_rejected",
                details={
                    "token_prefix": _token_prefix(presented_token),
                    "path": request.url.path,
                    "reason": "expired",
                },
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing authorization token",
            )

        request.state.caller_identity = CallerIdentity(
            username="anonymous", auth_mode="shared_secret"
        )

    return verify_bearer


SA_TOKEN_PATH = "/var/run/secrets/cloud-agents/token"


def get_runner_auth_token() -> Optional[str]:
    """Get the auth token for runner-to-agent calls based on AUTH_MODE.

    In shared_secret mode, returns AGENT_API_TOKEN.
    In sa_token mode, reads the projected SA token from the volume mount.

    Returns:
        Token string, or None if auth is disabled.
    """
    if get_auth_mode() == "sa_token":
        try:
            with open(SA_TOKEN_PATH) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None
    token = get_api_token()
    return token or None
