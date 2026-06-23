"""Kubernetes agent spawner — creates K8s Jobs for on-demand agents.

Production spawner. Uses the kubernetes Python client to create
Jobs with scoped ServiceAccounts and resource limits.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)


class KubernetesSpawner(AgentSpawner):
    """Spawns K8s Jobs for on-demand agents.

    Attributes:
        namespace: K8s namespace for spawned Jobs.
        service_account: ServiceAccount for spawned pods.
    """

    def __init__(
        self,
        namespace: str = "cloud-agents",
        service_account: str = "workflow-runner",
        **kwargs: Any,
    ) -> None:
        """Initialize the Kubernetes spawner.

        Args:
            namespace: K8s namespace for Jobs.
            service_account: ServiceAccount for spawned pods.
        """
        super().__init__(**kwargs)
        self._namespace = namespace
        self._service_account = service_account

    async def _do_spawn(self, agent_name: str, image: str, env: dict[str, str]) -> str:
        """Create a K8s Job for the agent.

        Returns the Service endpoint URL.
        """
        try:
            from kubernetes import client, config

            config.load_incluster_config()
            batch = client.BatchV1Api()
            core = client.CoreV1Api()
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to K8s API: {exc}") from exc

        job_name = f"agent-{agent_name}"
        env_list = [client.V1EnvVar(name=k, value=v) for k, v in env.items()]

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                labels={"app": agent_name, "spawned-by": "workflow-runner"},
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=300,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={"app": agent_name},
                    ),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        service_account_name=self._service_account,
                        containers=[
                            client.V1Container(
                                name="agent",
                                image=image,
                                env=env_list,
                                ports=[client.V1ContainerPort(container_port=8080)],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "100m", "memory": "256Mi"},
                                    limits={"cpu": "500m", "memory": "512Mi"},
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )

        batch.create_namespaced_job(namespace=self._namespace, body=job)

        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name=job_name),
            spec=client.V1ServiceSpec(
                selector={"app": agent_name},
                ports=[client.V1ServicePort(port=8080, target_port=8080)],
            ),
        )
        core.create_namespaced_service(namespace=self._namespace, body=svc)

        logger.info("Spawned K8s Job '%s' in namespace '%s'", job_name, self._namespace)
        return f"http://{job_name}.{self._namespace}.svc:8080"

    async def _do_destroy(self, agent_name: str) -> None:
        """Delete the K8s Job and Service."""
        try:
            from kubernetes import client, config

            config.load_incluster_config()
            batch = client.BatchV1Api()
            core = client.CoreV1Api()

            job_name = f"agent-{agent_name}"
            batch.delete_namespaced_job(
                name=job_name,
                namespace=self._namespace,
                propagation_policy="Background",
            )
            core.delete_namespaced_service(name=job_name, namespace=self._namespace)
            logger.info("Destroyed K8s Job '%s'", job_name)
        except Exception as exc:
            logger.warning("Failed to destroy K8s Job '%s': %s", agent_name, exc)
