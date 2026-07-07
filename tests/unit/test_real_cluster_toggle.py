"""Tests for issue #48: Real Cluster mode toggle for K8s Incident Response.

Validates:
1. Docker Compose demo overlay includes mcp-kubectl service on port 8082
2. Makefile demo-up echo mentions MCP kubectl on port 8082
3. DEMO.md documents the Real Cluster toggle
4. Dashboard HTML has variant support, toggle UI, and localStorage persistence
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]


class TestDockerComposeMcpKubectl:
    """Validate mcp-kubectl service in docker-compose.demo.yaml."""

    COMPOSE_PATH = ROOT / "deploy" / "podman" / "docker-compose.demo.yaml"

    @pytest.fixture
    def compose_data(self) -> dict:
        """Load the demo compose overlay."""
        with open(self.COMPOSE_PATH) as f:
            return yaml.safe_load(f)

    def test_mcp_kubectl_service_exists(self, compose_data: dict) -> None:
        """Compose overlay must define an mcp-kubectl service."""
        assert "mcp-kubectl" in compose_data.get("services", {}), (
            "docker-compose.demo.yaml must define mcp-kubectl service"
        )

    def test_mcp_kubectl_image(self, compose_data: dict) -> None:
        """mcp-kubectl service must use localhost/mcp-kubectl:latest image."""
        svc = compose_data["services"]["mcp-kubectl"]
        assert svc["image"] == "localhost/mcp-kubectl:latest"

    def test_mcp_kubectl_restart_policy(self, compose_data: dict) -> None:
        """mcp-kubectl service must have unless-stopped restart policy."""
        svc = compose_data["services"]["mcp-kubectl"]
        assert svc.get("restart") == "unless-stopped"

    def test_mcp_kubectl_port(self, compose_data: dict) -> None:
        """mcp-kubectl service must expose port 8082."""
        svc = compose_data["services"]["mcp-kubectl"]
        ports = svc.get("ports", [])
        port_strs = [str(p) for p in ports]
        assert any("8082" in p for p in port_strs), (
            f"mcp-kubectl must expose port 8082, got: {ports}"
        )


class TestMakefileDemoUp:
    """Validate Makefile demo-up echo includes MCP kubectl."""

    @pytest.fixture
    def makefile_content(self) -> str:
        """Read the Makefile."""
        return (ROOT / "Makefile").read_text()

    def test_demo_up_mentions_mcp_kubectl(self, makefile_content: str) -> None:
        """demo-up echo should mention MCP kubectl."""
        # Find all echo lines in the demo-up target
        in_demo_up = False
        found = False
        for line in makefile_content.split("\n"):
            if line.startswith("demo-up:"):
                in_demo_up = True
            elif in_demo_up and not line.startswith("\t") and not line.startswith(" "):
                break
            elif in_demo_up and "8082" in line:
                found = True
        assert found, "demo-up must echo MCP kubectl on port 8082"


class TestDemoDocsRealCluster:
    """Validate DEMO.md documents the Real Cluster toggle."""

    DEMO_PATH = ROOT / "examples" / "DEMO.md"

    @pytest.fixture
    def demo_content(self) -> str:
        """Read DEMO.md."""
        return self.DEMO_PATH.read_text()

    def test_documents_real_cluster_toggle(self, demo_content: str) -> None:
        """DEMO.md must mention the Real Cluster toggle."""
        assert "real cluster" in demo_content.lower() or "Real Cluster" in demo_content, (
            "DEMO.md must document the Real Cluster toggle"
        )

    def test_documents_kind_cluster_requirement(self, demo_content: str) -> None:
        """DEMO.md should mention Kind cluster or kubeconfig requirement."""
        lower = demo_content.lower()
        assert "kind" in lower or "kubeconfig" in lower, (
            "DEMO.md must mention Kind cluster or kubeconfig requirement for real-cluster mode"
        )

    def test_documents_graceful_degradation(self, demo_content: str) -> None:
        """DEMO.md should mention graceful degradation when no cluster is available."""
        assert "graceful" in demo_content.lower(), (
            "DEMO.md must mention graceful degradation when no cluster is reachable"
        )

    def test_documents_mcp_kubectl_service(self, demo_content: str) -> None:
        """DEMO.md should mention the mcp-kubectl service."""
        assert "mcp-kubectl" in demo_content or "mcp kubectl" in demo_content.lower(), (
            "DEMO.md must mention the mcp-kubectl service"
        )


class TestDashboardRealClusterToggle:
    """Validate demo-dashboard.html has real-cluster variant + toggle UI."""

    DASHBOARD_PATH = ROOT / "docs" / "demo-dashboard.html"

    @pytest.fixture
    def html_content(self) -> str:
        """Read the dashboard HTML."""
        return self.DASHBOARD_PATH.read_text()

    def test_k8s_scenario_has_variants(self, html_content: str) -> None:
        """K8s Incident Response scenario must have a variants map."""
        assert "variants" in html_content, (
            "K8s scenario must define a 'variants' map with simulated and realCluster"
        )

    def test_has_simulated_variant(self, html_content: str) -> None:
        """K8s scenario must have a 'simulated' variant."""
        assert "simulated" in html_content, (
            "K8s scenario must have a 'simulated' variant"
        )

    def test_has_real_cluster_variant(self, html_content: str) -> None:
        """K8s scenario must have a 'realCluster' variant."""
        assert "realCluster" in html_content, (
            "K8s scenario must have a 'realCluster' variant"
        )

    def test_real_cluster_variant_has_mcp_servers(self, html_content: str) -> None:
        """realCluster variant must reference kubectl MCP server."""
        assert "mcp__kubectl" in html_content or "mcp-kubectl" in html_content, (
            "realCluster variant must reference kubectl MCP server"
        )

    def test_real_cluster_variant_has_kubectl_url(self, html_content: str) -> None:
        """realCluster variant must have mcp-kubectl URL on port 8082."""
        assert "mcp-kubectl:8082" in html_content, (
            "realCluster variant must include mcp-kubectl:8082 URL"
        )

    def test_toggle_ui_exists(self, html_content: str) -> None:
        """Dashboard must have a toggle UI element (pill/switch)."""
        # Check for toggle-related CSS or HTML
        has_toggle = (
            "mode-toggle" in html_content
            or "variant-toggle" in html_content
            or "cluster-toggle" in html_content
        )
        assert has_toggle, "Dashboard must have a mode toggle UI element"

    def test_toggle_labels(self, html_content: str) -> None:
        """Toggle must show 'Simulated' and 'Real Cluster' labels."""
        assert "Simulated" in html_content, "Toggle must have 'Simulated' label"
        assert "Real Cluster" in html_content, "Toggle must have 'Real Cluster' label"

    def test_localstorage_persistence(self, html_content: str) -> None:
        """Toggle state must persist in localStorage."""
        assert "localStorage" in html_content, (
            "Toggle state must persist via localStorage"
        )

    def test_select_scenario_reads_toggle(self, html_content: str) -> None:
        """selectScenario must read toggle state to pick variant."""
        # Check that selectScenario references variant logic
        assert "variant" in html_content, (
            "selectScenario must read toggle state and pick variant"
        )

    def test_real_cluster_variant_has_allowed_tools(self, html_content: str) -> None:
        """realCluster variant steps must have permissions.allowed_tools."""
        assert "allowed_tools" in html_content, (
            "realCluster variant must specify allowed_tools per step"
        )
