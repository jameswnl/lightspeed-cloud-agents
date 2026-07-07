"""E2E test: network egress enforcement on Kind cluster with Calico CNI.

Validates that sandbox pods cannot reach arbitrary external hosts
when NetworkPolicy is enforced. Requires a Kind cluster with Calico
CNI installed and the network-policy.yaml applied.

Prerequisites:
  - Kind cluster with Calico CNI (see deploy/kind/kind-config-calico.yaml)
  - Network policy applied: kubectl apply -f deploy/kind/network-policy.yaml
  - EGRESS_TEST_ENABLED=1 in environment

Usage:
  EGRESS_TEST_ENABLED=1 uv run pytest tests/e2e/test_egress.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

SANDBOX_IMAGE = os.environ.get(
    "SANDBOX_IMAGE", "localhost/lightspeed-agentic-sandbox:temporal"
)


def _kubectl_run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a kubectl command and return the result.

    Parameters:
        args: Command arguments after 'kubectl'.
        timeout: Timeout in seconds.

    Returns:
        CompletedProcess with stdout/stderr.
    """
    return subprocess.run(
        ["kubectl"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _cluster_available() -> bool:
    """Check if a Kubernetes cluster is accessible."""
    try:
        result = _kubectl_run(["cluster-info"], timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _calico_installed() -> bool:
    """Check if Calico CNI is installed in the cluster."""
    try:
        result = _kubectl_run(
            ["get", "pods", "-n", "kube-system", "-l", "k8s-app=calico-node", "-o", "name"],
            timeout=10,
        )
        return result.returncode == 0 and "pod/" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@pytest.mark.skipif(
    not os.environ.get("EGRESS_TEST_ENABLED"),
    reason="Egress tests require Kind + Calico (set EGRESS_TEST_ENABLED=1)",
)
class TestNetworkEgress:
    """Verify sandbox pods cannot reach arbitrary external hosts."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_cluster(self) -> None:
        """Skip if no accessible Kubernetes cluster."""
        if not _cluster_available():
            pytest.skip("No accessible Kubernetes cluster")
        if not _calico_installed():
            pytest.skip("Calico CNI not installed (required for NetworkPolicy egress)")

    def test_sandbox_cannot_reach_external_http(self) -> None:
        """Sandbox pod cannot make HTTP requests to arbitrary external hosts.

        Runs a curl command inside a sandbox-like pod and expects it to
        fail (timeout or connection refused) due to NetworkPolicy.
        """
        pod_name = "egress-test-http"
        namespace = os.environ.get("EGRESS_TEST_NAMESPACE", "default")

        # Create a pod with sandbox labels so NetworkPolicy applies
        try:
            _kubectl_run([
                "run", pod_name,
                "--namespace", namespace,
                "--image", "curlimages/curl:latest",
                "--labels", "app=agent-sandbox,spawned-by=workflow-runner",
                "--restart", "Never",
                "--command", "--",
                "curl", "--connect-timeout", "5", "--max-time", "10",
                "http://example.com",
            ])

            # Wait for pod to complete
            _kubectl_run([
                "wait", "--for=condition=Ready",
                f"pod/{pod_name}", "--namespace", namespace,
                "--timeout=30s",
            ])

            # Check exit code - should be non-zero (connection failed)
            result = _kubectl_run([
                "get", "pod", pod_name, "--namespace", namespace,
                "-o", "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
            ], timeout=60)

            # Wait for the pod to terminate (Failed phase because egress is blocked)
            _kubectl_run([
                "wait", "--for=jsonpath={.status.phase}=Failed",
                f"pod/{pod_name}", "--namespace", namespace,
                "--timeout=60s",
            ])

            # Re-check exit code
            result = _kubectl_run([
                "get", "pod", pod_name, "--namespace", namespace,
                "-o", "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
            ])

            exit_code = result.stdout.strip()
            assert exit_code and exit_code != "0", (
                f"Expected curl to fail (egress blocked), but got exit code {exit_code!r}"
            )
        finally:
            _kubectl_run(["delete", "pod", pod_name, "--namespace", namespace, "--ignore-not-found"])

    def test_sandbox_can_reach_dns(self) -> None:
        """Sandbox pod can perform DNS resolution (port 53 allowed).

        NetworkPolicy should allow DNS egress so pods can resolve
        service names within the cluster.
        """
        pod_name = "egress-test-dns"
        namespace = os.environ.get("EGRESS_TEST_NAMESPACE", "default")

        try:
            _kubectl_run([
                "run", pod_name,
                "--namespace", namespace,
                "--image", "busybox:latest",
                "--labels", "app=agent-sandbox,spawned-by=workflow-runner",
                "--restart", "Never",
                "--command", "--",
                "nslookup", "kubernetes.default.svc.cluster.local",
            ])

            # Wait for pod to complete
            _kubectl_run([
                "wait", "--for=jsonpath={.status.phase}=Succeeded",
                f"pod/{pod_name}", "--namespace", namespace,
                "--timeout=30s",
            ])

            result = _kubectl_run([
                "get", "pod", pod_name, "--namespace", namespace,
                "-o", "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
            ])

            exit_code = result.stdout.strip()
            assert exit_code == "0", (
                f"Expected DNS lookup to succeed (DNS port 53 allowed), "
                f"but got exit code {exit_code}"
            )
        finally:
            _kubectl_run(["delete", "pod", pod_name, "--namespace", namespace, "--ignore-not-found"])

    def test_sandbox_can_reach_cluster_services(self) -> None:
        """Sandbox pod can reach in-cluster services (e.g., Kubernetes API).

        NetworkPolicy should allow egress to cluster-internal services
        so the sandbox can communicate with the workflow runner.
        """
        pod_name = "egress-test-internal"
        namespace = os.environ.get("EGRESS_TEST_NAMESPACE", "default")

        try:
            _kubectl_run([
                "run", pod_name,
                "--namespace", namespace,
                "--image", "curlimages/curl:latest",
                "--labels", "app=agent-sandbox,spawned-by=workflow-runner",
                "--restart", "Never",
                "--command", "--",
                "curl", "--connect-timeout", "5", "--max-time", "10",
                "-k", "https://kubernetes.default.svc:443/healthz",
            ])

            # Wait for pod to complete
            _kubectl_run([
                "wait", "--for=jsonpath={.status.phase}=Succeeded",
                f"pod/{pod_name}", "--namespace", namespace,
                "--timeout=60s",
            ])

            result = _kubectl_run([
                "get", "pod", pod_name, "--namespace", namespace,
                "-o", "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
            ])

            exit_code = result.stdout.strip()
            assert exit_code == "0", (
                f"Expected in-cluster HTTPS to succeed, "
                f"but got exit code {exit_code}"
            )
        finally:
            _kubectl_run(["delete", "pod", pod_name, "--namespace", namespace, "--ignore-not-found"])
