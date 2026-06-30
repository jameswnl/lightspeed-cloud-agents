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
        projected_sa_token: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the Kubernetes spawner.

        Args:
            namespace: K8s namespace for Jobs.
            service_account: ServiceAccount for spawned pods.
            config_configmap: ConfigMap name for agent.yaml + registry.yaml.
            tools_configmap: ConfigMap name for tool modules.
            projected_sa_token: Mount projected SA token for TokenReview auth.
        """
        super().__init__(**kwargs)
        self._namespace = namespace
        self._service_account = service_account
        self._config_configmap = config_configmap
        self._tools_configmap = tools_configmap
        self._secret_env_vars = secret_env_vars or {}
        self._projected_sa_token = projected_sa_token

    async def _do_spawn(
        self,
        agent_name: str,
        image: str,
        env: dict[str, str],
        config_override: "SpawnConfig | None" = None,
        labels: dict[str, str] | None = None,
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
    ) -> str:
        """Create a K8s Job for the agent.

        Returns the Service endpoint URL.
        """
        from agents.spawner.base import SpawnConfig

        cfg = config_override or SpawnConfig()

        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            batch = client.BatchV1Api()
            core = client.CoreV1Api()
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to K8s API: {exc}") from exc

        job_name = f"agent-{agent_name}"
        env_list = []
        sensitive_keys = set(self._secret_env_vars.keys())
        for k, v in env.items():
            if k in sensitive_keys:
                continue
            env_list.append(client.V1EnvVar(name=k, value=v))
        for env_name, ref in self._secret_env_vars.items():
            env_list.append(
                client.V1EnvVar(
                    name=env_name,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=ref.secret_name,
                            key=ref.key,
                        ),
                    ),
                )
            )

        volumes = [
            client.V1Volume(
                name="tmp-scratch",
                empty_dir=client.V1EmptyDirVolumeSource(medium="Memory"),
            ),
        ]
        volume_mounts = [
            client.V1VolumeMount(
                name="tmp-scratch",
                mount_path="/tmp",
            ),
        ]
        if self._config_configmap:
            volumes.append(
                client.V1Volume(
                    name="agent-config",
                    config_map=client.V1ConfigMapVolumeSource(
                        name=self._config_configmap
                    ),
                )
            )
            volume_mounts.extend(
                [
                    client.V1VolumeMount(
                        name="agent-config",
                        mount_path="/app/agent.yaml",
                        sub_path="agent.yaml",
                        read_only=True,
                    ),
                    client.V1VolumeMount(
                        name="agent-config",
                        mount_path="/app/registry.yaml",
                        sub_path="registry.yaml",
                        read_only=True,
                    ),
                ]
            )
        if self._tools_configmap:
            volumes.append(
                client.V1Volume(
                    name="agent-tools",
                    config_map=client.V1ConfigMapVolumeSource(
                        name=self._tools_configmap
                    ),
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="agent-tools", mount_path="/app/tools", read_only=True
                ),
            )

        job_labels = {"app": agent_name, "spawned-by": "workflow-runner"}
        if labels:
            job_labels.update(labels)
        pod_labels = {"app": agent_name}
        if labels:
            pod_labels.update(labels)

        init_containers = None
        if skills_image:
            copy_paths = skills_paths or ["/skills"]
            copy_cmd = " && ".join(f"cp -r {p} /skills-data/" for p in copy_paths)
            volumes.append(
                client.V1Volume(
                    name="skills-data",
                    empty_dir=client.V1EmptyDirVolumeSource(),
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="skills-data",
                    mount_path="/app/skills",
                )
            )
            init_containers = [
                client.V1Container(
                    name="skills-loader",
                    image=skills_image,
                    command=["sh", "-c", copy_cmd],
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="skills-data",
                            mount_path="/skills-data",
                        ),
                    ],
                ),
            ]

        if self._projected_sa_token:
            volumes.append(
                client.V1Volume(
                    name="sa-token",
                    projected=client.V1ProjectedVolumeSource(
                        sources=[
                            client.V1VolumeProjection(
                                service_account_token=client.V1ServiceAccountTokenProjection(
                                    audience="cloud-agents",
                                    expiration_seconds=3600,
                                    path="token",
                                ),
                            ),
                        ]
                    ),
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="sa-token",
                    mount_path="/var/run/secrets/cloud-agents",
                    read_only=True,
                )
            )

        env_from = None
        if credential_secret_name:
            volumes.append(
                client.V1Volume(
                    name="llm-credentials",
                    secret=client.V1SecretVolumeSource(
                        secret_name=credential_secret_name,
                    ),
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="llm-credentials",
                    mount_path="/var/run/secrets/llm-credentials/",
                    read_only=True,
                )
            )
            env_from = [
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(
                        name=credential_secret_name,
                    ),
                ),
            ]

        if mcp_secret_mounts:
            for secret_name, _key, mount_path in mcp_secret_mounts:
                vol_name = f"mcp-secret-{secret_name}"
                volumes.append(
                    client.V1Volume(
                        name=vol_name,
                        secret=client.V1SecretVolumeSource(
                            secret_name=secret_name,
                        ),
                    )
                )
                volume_mounts.append(
                    client.V1VolumeMount(
                        name=vol_name,
                        mount_path=mount_path,
                        read_only=True,
                    )
                )

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                labels=job_labels,
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=300,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=pod_labels,
                    ),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        service_account_name=service_account or self._service_account,
                        automount_service_account_token=False,
                        init_containers=init_containers,
                        containers=[
                            client.V1Container(
                                name="agent",
                                image=image,
                                image_pull_policy="IfNotPresent",
                                env=env_list,
                                env_from=env_from,
                                ports=[client.V1ContainerPort(container_port=8080)],
                                resources=client.V1ResourceRequirements(
                                    requests={
                                        "cpu": cfg.cpu_request,
                                        "memory": cfg.memory_request,
                                    },
                                    limits={
                                        "cpu": cfg.cpu_limit,
                                        "memory": cfg.memory_limit,
                                    },
                                ),
                                security_context=client.V1SecurityContext(
                                    run_as_non_root=True,
                                    read_only_root_filesystem=True,
                                    allow_privilege_escalation=False,
                                ),
                                volume_mounts=volume_mounts or None,
                            ),
                        ],
                        volumes=volumes or None,
                    ),
                ),
            ),
        )

        try:
            batch.create_namespaced_job(namespace=self._namespace, body=job)
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                existing = batch.read_namespaced_job(
                    name=job_name, namespace=self._namespace
                )
                existing_image = existing.spec.template.spec.containers[0].image
                if existing_image != image:
                    raise RuntimeError(
                        f"Job '{job_name}' exists with different image: "
                        f"{existing_image} vs {image}"
                    ) from exc
                logger.info("Job '%s' already exists (idempotent retry)", job_name)
            else:
                raise

        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name=job_name),
            spec=client.V1ServiceSpec(
                selector={"app": agent_name},
                ports=[client.V1ServicePort(port=8080, target_port=8080)],
            ),
        )
        try:
            core.create_namespaced_service(namespace=self._namespace, body=svc)
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                logger.info("Service '%s' already exists (idempotent retry)", job_name)
            else:
                raise

        logger.info("Spawned K8s Job '%s' in namespace '%s'", job_name, self._namespace)
        return f"http://{job_name}.{self._namespace}.svc:8080"

    async def _do_list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """List active K8s Jobs matching the given labels.

        Args:
            labels: Optional label selector to filter Jobs.

        Returns:
            List of agent names (without the "agent-" prefix).
        """
        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            batch = client.BatchV1Api()
        except Exception as exc:
            logger.warning("Cannot connect to K8s API for list_active: %s", exc)
            return []

        label_selector = ",".join(f"{k}={v}" for k, v in (labels or {}).items())
        jobs = batch.list_namespaced_job(
            namespace=self._namespace,
            label_selector=label_selector,
        )
        return [job.metadata.name.removeprefix("agent-") for job in jobs.items]

    async def _do_destroy(self, agent_name: str) -> None:
        """Delete the K8s Job and Service."""
        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
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
