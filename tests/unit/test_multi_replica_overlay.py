"""Validate the 2-replica Kind deployment overlay YAML.

Ensures the overlay has the correct structure:
- 2 replicas specified
- Same app label as base workflow-runner
- Same container spec (image, ports, security context)
- Valid Kubernetes Deployment manifest
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

OVERLAY_PATH = Path(__file__).parents[2] / "deploy" / "kind" / "workflow-runner-2-replicas.yaml"
BASE_PATH = Path(__file__).parents[2] / "deploy" / "kind" / "workflow-runner.yaml"


class TestMultiReplicaOverlay:
    """Validate the 2-replica Kind deployment overlay."""

    @pytest.fixture
    def overlay(self) -> dict:
        """Load the 2-replica overlay YAML."""
        assert OVERLAY_PATH.exists(), f"Overlay not found: {OVERLAY_PATH}"
        with open(OVERLAY_PATH) as f:
            docs = list(yaml.safe_load_all(f))
        # Find the Deployment document
        deployments = [d for d in docs if d.get("kind") == "Deployment"]
        assert len(deployments) == 1, "Expected exactly one Deployment in overlay"
        return deployments[0]

    @pytest.fixture
    def base(self) -> dict:
        """Load the base workflow-runner YAML."""
        assert BASE_PATH.exists(), f"Base not found: {BASE_PATH}"
        with open(BASE_PATH) as f:
            docs = list(yaml.safe_load_all(f))
        deployments = [d for d in docs if d.get("kind") == "Deployment"]
        assert len(deployments) == 1
        return deployments[0]

    def test_overlay_is_valid_yaml(self) -> None:
        """Overlay file parses as valid YAML."""
        assert OVERLAY_PATH.exists()
        with open(OVERLAY_PATH) as f:
            docs = list(yaml.safe_load_all(f))
        assert len(docs) >= 1

    def test_overlay_has_two_replicas(self, overlay: dict) -> None:
        """Overlay specifies exactly 2 replicas."""
        assert overlay["spec"]["replicas"] == 2

    def test_overlay_is_deployment_kind(self, overlay: dict) -> None:
        """Overlay apiVersion and kind are correct."""
        assert overlay["apiVersion"] == "apps/v1"
        assert overlay["kind"] == "Deployment"

    def test_overlay_has_same_app_label(self, overlay: dict, base: dict) -> None:
        """Overlay uses the same app label as the base deployment."""
        overlay_label = overlay["metadata"]["labels"]["app"]
        base_label = base["metadata"]["labels"]["app"]
        assert overlay_label == base_label == "workflow-runner"

    def test_overlay_has_same_selector(self, overlay: dict, base: dict) -> None:
        """Overlay uses the same selector as the base deployment."""
        overlay_sel = overlay["spec"]["selector"]["matchLabels"]["app"]
        base_sel = base["spec"]["selector"]["matchLabels"]["app"]
        assert overlay_sel == base_sel == "workflow-runner"

    def test_overlay_container_has_security_context(self, overlay: dict) -> None:
        """Overlay container has security context enforced."""
        container = overlay["spec"]["template"]["spec"]["containers"][0]
        sc = container["securityContext"]
        assert sc["runAsNonRoot"] is True
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["allowPrivilegeEscalation"] is False

    def test_overlay_container_has_readiness_probe(self, overlay: dict) -> None:
        """Overlay container has a readiness probe."""
        container = overlay["spec"]["template"]["spec"]["containers"][0]
        probe = container["readinessProbe"]
        assert probe["httpGet"]["path"] == "/healthz"
        assert probe["httpGet"]["port"] == 8080

    def test_overlay_has_resource_limits(self, overlay: dict) -> None:
        """Overlay container has resource requests and limits."""
        container = overlay["spec"]["template"]["spec"]["containers"][0]
        resources = container["resources"]
        assert "requests" in resources
        assert "limits" in resources
        assert "cpu" in resources["requests"]
        assert "memory" in resources["requests"]

    def test_overlay_uses_same_task_queue(self, overlay: dict, base: dict) -> None:
        """Both replicas connect to the same Temporal task queue.

        The TEMPORAL_URL and TEMPORAL_NAMESPACE env vars must match.
        """
        overlay_envs = {
            e["name"]: e.get("value") or e.get("valueFrom")
            for e in overlay["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        base_envs = {
            e["name"]: e.get("value") or e.get("valueFrom")
            for e in base["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert overlay_envs["TEMPORAL_URL"] == base_envs["TEMPORAL_URL"]
        assert overlay_envs["TEMPORAL_NAMESPACE"] == base_envs["TEMPORAL_NAMESPACE"]

    def test_overlay_has_service_account(self, overlay: dict, base: dict) -> None:
        """Overlay uses the same service account as base."""
        overlay_sa = overlay["spec"]["template"]["spec"]["serviceAccountName"]
        base_sa = base["spec"]["template"]["spec"]["serviceAccountName"]
        assert overlay_sa == base_sa

    def test_overlay_has_same_image(self, overlay: dict, base: dict) -> None:
        """Overlay uses the same container image as base."""
        overlay_img = overlay["spec"]["template"]["spec"]["containers"][0]["image"]
        base_img = base["spec"]["template"]["spec"]["containers"][0]["image"]
        assert overlay_img == base_img

    def test_overlay_only_differs_in_replicas(self, overlay: dict, base: dict) -> None:
        """Overlay is identical to base except for replicas count.

        Catches drift: if the base is edited without updating the overlay,
        this test fails.
        """
        import copy

        expected = copy.deepcopy(base)
        expected["spec"]["replicas"] = 2
        assert overlay == expected

    def test_overlay_includes_service(self) -> None:
        """Overlay file includes a Service document alongside the Deployment."""
        with open(OVERLAY_PATH) as f:
            docs = list(yaml.safe_load_all(f))
        services = [d for d in docs if d.get("kind") == "Service"]
        assert len(services) == 1, "Expected exactly one Service in overlay"
        assert services[0]["spec"]["selector"]["app"] == "workflow-runner"
        assert services[0]["spec"]["ports"][0]["port"] == 8080
