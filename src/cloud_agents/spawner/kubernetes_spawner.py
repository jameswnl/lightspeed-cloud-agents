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
        config_configmap: str | None = None,
        tools_configmap: str | None = None,
        secret_env_vars: dict[str, "SecretKeyRef"] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Kubernetes spawner.

        Args:
            namespace: K8s namespace for Jobs.
            service_account: ServiceAccount for spawned pods.
            config_configmap: ConfigMap name for agent.yaml + registry.yaml.
            tools_configmap: ConfigMap name for tool modules.
        """
        super().__init__(**kwargs)
        self._namespace = namespace
        self._service_account = service_account
        self._config_configmap = config_configmap
        self._tools_configmap = tools_configmap
        self._secret_env_vars = secret_env_vars or {}

    async def _do_spawn(
        self, agent_name: str, image: str, env: dict[str, str],
        config_override: "SpawnConfig | None" = None,
    ) -> str:
        """Create a K8s Job for the agent.

        Returns the Service endpoint URL.
        """
        from agents.spawner.base import SpawnConfig

        cfg = config_override or SpawnConfig()

        try:
            from kubernetes import client, config

            config.load_incluster_config()
            batch = client.BatchV1Api()
            core = client.CoreV1Api()
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to K8s API: {exc}") from exc

        from agents.spawner.base import SecretKeyRef

        job_name = f"agent-{agent_name}"
        env_list = []
        sensitive_keys = set(self._secret_env_vars.keys())
        for k, v in env.items():
            if k in sensitive_keys:
                continue
            env_list.append(client.V1EnvVar(name=k, value=v))
        for env_name, ref in self._secret_env_vars.items():
            env_list.append(client.V1EnvVar(
                name=env_name,
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=ref.secret_name, key=ref.key,
                    ),
                ),
            ))

        volumes = []
        volume_mounts = []
        if self._config_configmap:
            volumes.append(client.V1Volume(
                name="agent-config",
                config_map=client.V1ConfigMapVolumeSource(name=self._config_configmap),
            ))
            volume_mounts.extend([
                client.V1VolumeMount(name="agent-config", mount_path="/app/agent.yaml", sub_path="agent.yaml", read_only=True),
                client.V1VolumeMount(name="agent-config", mount_path="/app/registry.yaml", sub_path="registry.yaml", read_only=True),
            ])
        if self._tools_configmap:
            volumes.append(client.V1Volume(
                name="agent-tools",
                config_map=client.V1ConfigMapVolumeSource(name=self._tools_configmap),
            ))
            volume_mounts.append(
                client.V1VolumeMount(name="agent-tools", mount_path="/app/tools", read_only=True),
            )

        # Projected SA token for per-pod identity (K8s production auth)
        volumes.append(client.V1Volume(
            name="agent-token",
            projected=client.V1ProjectedVolumeSource(
                sources=[client.V1VolumeProjection(
                    service_account_token=client.V1ServiceAccountTokenProjection(
                        path="agent-token",
                        expiration_seconds=3600,
                        audience="cloud-agents",
                    ),
                )],
            ),
        ))
        volume_mounts.append(
            client.V1VolumeMount(name="agent-token", mount_path="/var/run/secrets/tokens", read_only=True),
        )

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
                                image_pull_policy="Never",
                                env=env_list,
                                ports=[client.V1ContainerPort(container_port=8080)],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": cfg.cpu_request, "memory": cfg.memory_request},
                                    limits={"cpu": cfg.cpu_limit, "memory": cfg.memory_limit},
                                ),
                                volume_mounts=volume_mounts or None,
                            ),
                        ],
                        volumes=volumes or None,
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
