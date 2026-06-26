"""Agent runtime result callback.

Posts agent run results to the workflow runner's ingest endpoint
when RESULT_CALLBACK_URL is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class ResultCallback:
    """Posts agent run results to the workflow runner's ingest endpoint."""

    def __init__(
        self,
        callback_url: str,
        auth_token: str | None = None,
        attempt: int = 1,
        max_retries: int = 3,
    ) -> None:
        """Initialize the callback.

        Args:
            callback_url: Full URL of the ingest endpoint.
            auth_token: Bearer token for authentication.
            attempt: Current attempt number for the step.
            max_retries: Maximum retry count on transient failures.
        """
        self._callback_url = callback_url
        self._auth_token = auth_token
        self._attempt = attempt
        self._max_retries = max_retries

    async def post_result(
        self,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """POST the result to the callback URL with retries.

        Args:
            status: Step outcome — "completed" or "failed".
            output: Agent output data.
            error: Error message on failure.

        Returns:
            True if the callback succeeded, False otherwise.
        """
        from datetime import datetime, timezone

        payload = {
            "status": status,
            "output": output,
            "error": error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "attempt": self._attempt,
        }

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        for retry in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                    response = await client.post(
                        self._callback_url,
                        json=payload,
                        headers=headers,
                    )
                if response.status_code in (200, 409):
                    logger.info("Callback posted to %s (status=%d)", self._callback_url, response.status_code)
                    return True
                logger.warning(
                    "Callback to %s returned %d (attempt %d/%d)",
                    self._callback_url, response.status_code, retry + 1, self._max_retries,
                )
            except Exception as exc:
                logger.warning(
                    "Callback to %s failed (attempt %d/%d): %s",
                    self._callback_url, retry + 1, self._max_retries, exc,
                )

            if retry < self._max_retries - 1:
                backoff = 2 ** retry
                await asyncio.sleep(backoff)

        logger.error("Callback to %s failed after %d attempts", self._callback_url, self._max_retries)
        return False


def _get_auth_token() -> str | None:
    """Get the auth token for callbacks based on AUTH_MODE."""
    auth_mode = os.environ.get("AUTH_MODE", "shared_secret")
    if auth_mode == "sa_token":
        token_path = "/var/run/secrets/cloud-agents/token"
        try:
            with open(token_path) as f:
                return f.read().strip()
        except FileNotFoundError:
            logger.warning("Projected SA token not found at %s", token_path)
            return None
    return os.environ.get("AGENT_API_TOKEN")


def get_callback() -> Optional[ResultCallback]:
    """Create a ResultCallback from environment variables if configured."""
    url = os.environ.get("RESULT_CALLBACK_URL")
    if not url:
        return None
    return ResultCallback(
        callback_url=url,
        auth_token=_get_auth_token(),
        attempt=int(os.environ.get("RESULT_CALLBACK_ATTEMPT", "1")),
    )
