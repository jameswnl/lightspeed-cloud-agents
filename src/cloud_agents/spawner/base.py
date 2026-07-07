"""Abstract base class for agent pod spawning.

Defines the interface for creating and destroying agent pods on demand.
Implementations for Kubernetes and Podman deployment targets.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod

import re

import httpx
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

MAX_SPAWNED_PODS = int(os.environ.get("MAX_SPAWNED_PODS", "10"))


class SecretKeyRef(BaseModel):
    """Reference to a K8s Secret key for sensitive env vars.

    Attributes:
        secret_name: Name of the K8s Secret.
        key: Key within the Secret.
    """

    secret_name: str
    key: str


class SpawnConfig(BaseModel):
    """Per-step resource configuration for ephemeral pods.

    Validation bounds prevent resource abuse. workflow step configuration
    sets the maximum envelope; SpawnConfig may only narrow within it.

    Attributes:
        cpu_request: CPU request (e.g. "100m").
        cpu_limit: CPU limit (max 4 cores).
        memory_request: Memory request (e.g. "256Mi").
        memory_limit: Memory limit (max 4Gi).
        timeout_seconds: Max wait for pod readiness.
        health_path: Health probe endpoint path.
    """

    cpu_request: str = "100m"
    cpu_limit: str = Field(default="500m")
    memory_request: str = "256Mi"
    memory_limit: str = Field(default="512Mi")
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    health_path: str = "/healthz"

    @field_validator("cpu_limit")
    @classmethod
    def validate_cpu_limit(cls, v: str) -> str:
        """Reject CPU limits above 4 cores."""
        match = re.match(r"^(\d+)(m?)$", v)
        if not match:
            raise ValueError(f"Invalid cpu_limit format: {v}")
        value, unit = int(match.group(1)), match.group(2)
        cores = value / 1000 if unit == "m" else value
        if cores > 4:
            raise ValueError(f"cpu_limit {v} exceeds maximum of 4 cores")
        return v

    @field_validator("memory_limit")
    @classmethod
    def validate_memory_limit(cls, v: str) -> str:
        """Reject memory limits above 4Gi."""
        match = re.match(r"^(\d+)(Mi|Gi)$", v)
        if not match:
            raise ValueError(f"Invalid memory_limit format: {v}")
        value, unit = int(match.group(1)), match.group(2)
        mib = value if unit == "Mi" else value * 1024
        if mib > 4096:
            raise ValueError(f"memory_limit {v} exceeds maximum of 4Gi")
        return v


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

    async def spawn(
        self,
        agent_name: str,
        image: str,
        env: dict[str, str] | None = None,
        config: SpawnConfig | None = None,
        labels: dict[str, str] | None = None,
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: "EphemeralCerts | None" = None,
    ) -> str:
        """Spawn an agent pod and return its endpoint URL.

        Args:
            agent_name: Name for the spawned pod.
            image: Container image to use.
            env: Environment variables for the pod.
            config: Optional per-step resource configuration.
            labels: Optional metadata labels for the spawned resource.
            skills_image: Optional OCI image containing skills to mount.
            skills_paths: Paths within skills image to copy.
            service_account: Override ServiceAccount for this pod.
            read_only: Run container with read-only filesystem (advisory mode).
            credential_secret_name: K8s Secret to mount as volume and envFrom
                for file-based credential providers (e.g. Vertex, Bedrock).
            mcp_secret_mounts: List of (secret_name, key, mount_path) tuples
                for MCP server Secret-backed auth headers.
            tls_certs: Optional ephemeral TLS certs to inject into sandbox.

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
            endpoint = await self._do_spawn(
                agent_name,
                image,
                env or {},
                config,
                labels,
                skills_image=skills_image,
                skills_paths=skills_paths,
                service_account=service_account,
                read_only=read_only,
                credential_secret_name=credential_secret_name,
                mcp_secret_mounts=mcp_secret_mounts,
                tls_certs=tls_certs,
            )
            return endpoint
        except Exception:
            async with self._lock:
                self._active_count -= 1
            raise

    @abstractmethod
    async def _do_spawn(
        self,
        agent_name: str,
        image: str,
        env: dict[str, str],
        config: SpawnConfig | None = None,
        labels: dict[str, str] | None = None,
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: "EphemeralCerts | None" = None,
    ) -> str:
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

    async def list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """List active spawned agent names matching the given labels.

        Args:
            labels: Optional label selector to filter results.

        Returns:
            List of agent names (without the "agent-" prefix) that are
            currently active and match the given labels.
        """
        return await self._do_list_active(labels)

    @abstractmethod
    async def _do_list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """Implementation-specific listing of active agents."""

    async def read_file(self, agent_name: str, path: str) -> str:
        """Read a file from a spawned agent container.

        Parameters:
            agent_name: Name of the agent container.
            path: Absolute file path inside the container.

        Returns:
            File contents as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        return await self._do_read_file(agent_name, path)

    @abstractmethod
    async def _do_read_file(self, agent_name: str, path: str) -> str:
        """Implementation-specific file read from container."""

    async def write_file(self, agent_name: str, path: str, content: str) -> None:
        """Write content to a file inside a spawned agent container.

        Parameters:
            agent_name: Name of the agent container.
            path: Absolute file path inside the container.
            content: String content to write.

        Raises:
            RuntimeError: If the write operation fails.
        """
        await self._do_write_file(agent_name, path, content)

    @abstractmethod
    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        """Implementation-specific file write to container."""

    async def wait_ready(
        self,
        endpoint: str,
        timeout: float = 60.0,
        health_path: str = "/healthz",
        ca_cert_pem: bytes | None = None,
    ) -> bool:
        """Wait for a spawned pod to be ready.

        Polls the health endpoint until it returns 200 or timeout.

        Args:
            endpoint: HTTP endpoint of the pod.
            timeout: Maximum wait time in seconds.
            health_path: Health check path (default /healthz).
            ca_cert_pem: PEM-encoded CA certificate for TLS verification.
                When provided, creates an SSL context for HTTPS endpoints
                using self-signed certificates.

        Returns:
            True if the pod became ready, False if timed out.
        """
        import ssl
        import time

        client_kwargs: dict[str, object] = {"timeout": 5.0}
        if ca_cert_pem is not None:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.load_verify_locations(cadata=ca_cert_pem.decode())
            client_kwargs["verify"] = ssl_ctx

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.get(f"{endpoint}{health_path}")
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
