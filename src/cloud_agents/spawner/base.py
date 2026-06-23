"""Abstract base class for agent pod spawning.

Defines the interface for creating and destroying agent pods on demand.
Implementations for Kubernetes (production) and Podman (dev/test).
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MAX_SPAWNED_PODS = int(os.environ.get("MAX_SPAWNED_PODS", "10"))


class AgentSpawner(ABC):
    """Abstract interface for spawning agent pods on demand.

    Attributes:
        _active_count: Number of currently active spawned pods.
        _max_pods: Maximum concurrent spawned pods.
    """

    def __init__(self, max_pods: int = MAX_SPAWNED_PODS) -> None:
        """Initialize the spawner.

        Args:
            max_pods: Maximum number of concurrent spawned pods.
        """
        self._active_count = 0
        self._max_pods = max_pods
        self._lock = asyncio.Lock()

    async def spawn(self, agent_name: str, image: str, env: dict[str, str] | None = None) -> str:
        """Spawn an agent pod and return its endpoint URL.

        Args:
            agent_name: Name for the spawned pod.
            image: Container image to use.
            env: Environment variables for the pod.

        Returns:
            HTTP endpoint URL of the spawned pod.

        Raises:
            RuntimeError: If the concurrency cap is reached.
        """
        async with self._lock:
            if self._active_count >= self._max_pods:
                raise RuntimeError(
                    f"Concurrency cap reached: {self._active_count}/{self._max_pods} pods active"
                )
            self._active_count += 1

        try:
            endpoint = await self._do_spawn(agent_name, image, env or {})
            return endpoint
        except Exception:
            async with self._lock:
                self._active_count -= 1
            raise

    @abstractmethod
    async def _do_spawn(self, agent_name: str, image: str, env: dict[str, str]) -> str:
        """Implementation-specific pod creation."""

    async def destroy(self, agent_name: str) -> None:
        """Destroy a spawned agent pod.

        Args:
            agent_name: Name of the pod to destroy.
        """
        try:
            await self._do_destroy(agent_name)
        finally:
            async with self._lock:
                self._active_count = max(0, self._active_count - 1)

    @abstractmethod
    async def _do_destroy(self, agent_name: str) -> None:
        """Implementation-specific pod destruction."""

    async def wait_ready(self, endpoint: str, timeout: float = 60.0) -> bool:
        """Wait for a spawned pod to be ready.

        Polls /healthz until it returns 200 or timeout.

        Args:
            endpoint: HTTP endpoint of the pod.
            timeout: Maximum wait time in seconds.

        Returns:
            True if the pod became ready, False if timed out.
        """
        import time

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{endpoint}/healthz")
                    if resp.status_code == 200:
                        return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
        return False

    @property
    def active_count(self) -> int:
        """Number of currently active spawned pods."""
        return self._active_count
