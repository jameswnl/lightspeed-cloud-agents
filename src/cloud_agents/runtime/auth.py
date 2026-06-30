"""Authentication middleware for agent and workflow endpoints.

Validates bearer tokens on protected endpoints. Health and liveness
probes are exempt. Token is configured via AGENT_API_TOKEN env var.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

EXEMPT_PATHS = {"/healthz", "/livez", "/metrics"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Middleware that validates Bearer token on non-exempt endpoints.

    Attributes:
        token: The expected bearer token. If empty, auth is disabled.
    """

    def __init__(self, app: object, token: str = "") -> None:
        """Initialize with the expected token.

        Args:
            app: The ASGI application.
            token: Expected bearer token. Empty string disables auth.
        """
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: object) -> object:
        """Check authorization on non-exempt paths."""
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        if not self.token:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {self.token}":
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing authorization token"},
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
        if not await self._validate_token(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Token validation failed"},
            )

        return await call_next(request)

    async def _validate_token(self, token: str) -> bool:
        """Call K8s TokenReview API to validate the token."""
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
            return result.status.authenticated
        except Exception:
            return False


def get_auth_mode() -> str:
    """Get the authentication mode from environment.

    Returns:
        'shared_secret' (default) or 'sa_token'.
    """
    return os.environ.get("AUTH_MODE", "shared_secret")


def get_api_token() -> str:
    """Get the API token from environment.

    Both Podman and K8s use AGENT_API_TOKEN — injected via env var
    (Podman) or K8s Secret secretKeyRef (K8s). The same shared
    token is used by all pods in the deployment.

    Returns:
        Token string. Empty string means auth is disabled.
    """
    return os.environ.get("AGENT_API_TOKEN", "")


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
