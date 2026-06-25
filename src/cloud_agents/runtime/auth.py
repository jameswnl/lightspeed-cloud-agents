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


SA_TOKEN_PATH = "/var/run/secrets/tokens/agent-token"


def get_api_token() -> str:
    """Get the API token from environment or projected SA token file.

    Checks in order:
    1. AGENT_API_TOKEN env var (Podman / shared secret mode)
    2. Projected SA token file at /var/run/secrets/tokens/agent-token (K8s mode)
    3. Empty string (auth disabled)

    Returns:
        Token string. Empty string means auth is disabled.
    """
    env_token = os.environ.get("AGENT_API_TOKEN", "")
    if env_token:
        return env_token

    if os.path.exists(SA_TOKEN_PATH):
        try:
            with open(SA_TOKEN_PATH) as f:
                token = f.read().strip()
            if token:
                return token
        except Exception:
            pass

    return ""
