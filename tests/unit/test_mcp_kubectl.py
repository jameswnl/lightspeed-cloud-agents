"""Tests for K8s MCP kubectl server manifests and configuration.

Validates:
1. Containerfile exists and follows the established pattern
2. K8s manifests (Deployment, Service, ServiceAccount, RBAC) are correct
3. Network policy allows sandbox egress to mcp-kubectl
4. Workflow definition with tool filtering validates against schema
5. Makefile targets exist for build and deployment
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cloud_agents.workflow.definition import WorkflowDefinition
from cloud_agents.workflow.permissions import PermissionScope
from cloud_agents.workflow.temporal_validation import validate_definition

ROOT = Path(__file__).parents[2]


class TestContainerfile:
    """Validate the mcp-kubectl Containerfile."""

    CONTAINERFILE = ROOT / "deploy" / "mcp-kubectl" / "Containerfile"

    def test_containerfile_exists(self) -> None:
        """Containerfile for mcp-kubectl must exist."""
        assert self.CONTAINERFILE.exists(), (
            f"Missing {self.CONTAINERFILE} — "
            "deploy/mcp-kubectl/Containerfile should mirror deploy/mcp-filesystem/Containerfile"
        )

    def test_containerfile_uses_node_base(self) -> None:
        """Containerfile should use a Node.js base image (same pattern as mcp-filesystem)."""
        content = self.CONTAINERFILE.read_text()
        assert "FROM" in content
        assert "node" in content.lower(), "Should use a Node.js base image"

    def test_containerfile_installs_mcp_kubernetes(self) -> None:
        """Containerfile must install mcp-server-kubernetes."""
        content = self.CONTAINERFILE.read_text()
        assert "mcp-server-kubernetes" in content, (
            "Must install mcp-server-kubernetes npm package"
        )

    def test_containerfile_installs_supergateway(self) -> None:
        """Containerfile must install supergateway for streamable HTTP."""
        content = self.CONTAINERFILE.read_text()
        assert "supergateway" in content, (
            "Must install supergateway for stdio-to-HTTP bridge"
        )

    def test_containerfile_exposes_port_8082(self) -> None:
        """Containerfile must expose port 8082."""
        content = self.CONTAINERFILE.read_text()
        assert "8082" in content, "Must expose port 8082"

    def test_containerfile_runs_as_non_root(self) -> None:
        """Containerfile must set a non-root USER."""
        content = self.CONTAINERFILE.read_text()
        assert "USER" in content, (
            "Containerfile must contain a USER directive to avoid running as root"
        )


class TestKubernetesManifests:
    """Validate the mcp-kubectl K8s manifests."""

    MANIFEST_PATH = ROOT / "examples" / "kind-mcp-kubectl.yaml"

    @pytest.fixture
    def manifests(self) -> list[dict]:
        """Load all YAML documents from the manifest file."""
        assert self.MANIFEST_PATH.exists(), (
            f"Missing {self.MANIFEST_PATH} — "
            "should contain Deployment + Service + ServiceAccount + Role + RoleBinding"
        )
        with open(self.MANIFEST_PATH) as f:
            return list(yaml.safe_load_all(f))

    def _find_by_kind(self, manifests: list[dict], kind: str) -> dict | None:
        """Find a manifest document by its kind."""
        for doc in manifests:
            if doc and doc.get("kind") == kind:
                return doc
        return None

    def test_has_deployment(self, manifests: list[dict]) -> None:
        """Manifest must include a Deployment for mcp-kubectl."""
        dep = self._find_by_kind(manifests, "Deployment")
        assert dep is not None, "Missing Deployment manifest"
        assert dep["metadata"]["name"] == "mcp-kubectl"

    def test_deployment_uses_correct_image(self, manifests: list[dict]) -> None:
        """Deployment must use localhost/mcp-kubectl:latest image."""
        dep = self._find_by_kind(manifests, "Deployment")
        assert dep is not None
        container = dep["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "localhost/mcp-kubectl:latest"

    def test_deployment_uses_port_8082(self, manifests: list[dict]) -> None:
        """Deployment container must expose port 8082."""
        dep = self._find_by_kind(manifests, "Deployment")
        assert dep is not None
        container = dep["spec"]["template"]["spec"]["containers"][0]
        ports = [p["containerPort"] for p in container["ports"]]
        assert 8082 in ports

    def test_has_service(self, manifests: list[dict]) -> None:
        """Manifest must include a Service for mcp-kubectl."""
        svc = self._find_by_kind(manifests, "Service")
        assert svc is not None, "Missing Service manifest"
        assert svc["metadata"]["name"] == "mcp-kubectl"
        svc_ports = [p["port"] for p in svc["spec"]["ports"]]
        assert 8082 in svc_ports

    def test_has_service_account(self, manifests: list[dict]) -> None:
        """Manifest must include a ServiceAccount named mcp-kubectl."""
        sa = self._find_by_kind(manifests, "ServiceAccount")
        assert sa is not None, "Missing ServiceAccount manifest"
        assert sa["metadata"]["name"] == "mcp-kubectl"

    def test_has_role(self, manifests: list[dict]) -> None:
        """Manifest must include a Role with read access to key resources."""
        role = self._find_by_kind(manifests, "Role")
        assert role is not None, "Missing Role manifest"

        # Collect all rules
        all_resources = set()
        all_verbs = set()
        for rule in role["rules"]:
            for r in rule.get("resources", []):
                all_resources.add(r)
            for v in rule.get("verbs", []):
                all_verbs.add(v)

        # Must have read access to these resources
        required_read_resources = {"pods", "pods/log", "events", "deployments", "services", "nodes"}
        assert required_read_resources.issubset(all_resources), (
            f"Missing read resources: {required_read_resources - all_resources}"
        )
        assert {"get", "list", "watch"}.issubset(all_verbs), "Must have get/list/watch verbs"

    def test_role_has_limited_write(self, manifests: list[dict]) -> None:
        """Role must allow limited write access (patch for rollout restart, scale)."""
        role = self._find_by_kind(manifests, "Role")
        assert role is not None

        # Find rules with patch verb
        patch_rules = [r for r in role["rules"] if "patch" in r.get("verbs", [])]
        assert len(patch_rules) > 0, "Must have at least one rule with 'patch' verb"

        patch_resources = set()
        for rule in patch_rules:
            for r in rule.get("resources", []):
                patch_resources.add(r)
        assert "deployments" in patch_resources, "Must allow patch on deployments"

    def test_has_role_binding(self, manifests: list[dict]) -> None:
        """Manifest must include a RoleBinding linking SA to Role."""
        rb = self._find_by_kind(manifests, "RoleBinding")
        assert rb is not None, "Missing RoleBinding manifest"

        # Check subject references mcp-kubectl SA
        subjects = rb.get("subjects", [])
        sa_names = [s["name"] for s in subjects if s["kind"] == "ServiceAccount"]
        assert "mcp-kubectl" in sa_names

    def test_deployment_uses_service_account(self, manifests: list[dict]) -> None:
        """Deployment must mount the mcp-kubectl ServiceAccount."""
        dep = self._find_by_kind(manifests, "Deployment")
        assert dep is not None
        sa = dep["spec"]["template"]["spec"].get("serviceAccountName")
        assert sa == "mcp-kubectl", f"Expected serviceAccountName=mcp-kubectl, got {sa}"

    def test_deployment_has_readiness_probe(self, manifests: list[dict]) -> None:
        """Deployment container must have a readiness probe on port 8082."""
        dep = self._find_by_kind(manifests, "Deployment")
        assert dep is not None
        container = dep["spec"]["template"]["spec"]["containers"][0]
        probe = container.get("readinessProbe")
        assert probe is not None, "Missing readinessProbe"
        assert probe.get("tcpSocket", {}).get("port") == 8082

    def test_environment_label(self, manifests: list[dict]) -> None:
        """All manifests should have environment: cloud-agents label."""
        for doc in manifests:
            if doc and "metadata" in doc:
                labels = doc["metadata"].get("labels", {})
                if doc["kind"] in ("Deployment", "Service", "ServiceAccount"):
                    assert labels.get("environment") == "cloud-agents", (
                        f"{doc['kind']} {doc['metadata']['name']} missing environment label"
                    )


class TestNetworkPolicy:
    """Validate sandbox egress to mcp-kubectl in network policy."""

    POLICY_PATH = ROOT / "deploy" / "kind" / "network-policy.yaml"

    @pytest.fixture
    def policies(self) -> list[dict]:
        """Load all NetworkPolicy documents."""
        with open(self.POLICY_PATH) as f:
            return list(yaml.safe_load_all(f))

    def test_sandbox_egress_includes_mcp_kubectl_port(self, policies: list[dict]) -> None:
        """Sandbox egress policy must allow traffic to port 8082 (mcp-kubectl)."""
        sandbox_policy = None
        for doc in policies:
            if (
                doc
                and doc.get("kind") == "NetworkPolicy"
                and doc["metadata"].get("name") == "sandbox-egress"
            ):
                sandbox_policy = doc
                break

        assert sandbox_policy is not None, "Missing sandbox-egress NetworkPolicy"

        # Check egress rules include port 8082
        egress_ports = set()
        for rule in sandbox_policy["spec"].get("egress", []):
            for port_spec in rule.get("ports", []):
                egress_ports.add(port_spec.get("port"))

        assert 8082 in egress_ports, (
            f"sandbox-egress must allow port 8082 for mcp-kubectl. "
            f"Current ports: {egress_ports}"
        )

    def test_sandbox_egress_8082_scoped_to_mcp_kubectl(self, policies: list[dict]) -> None:
        """Port 8082 egress rule must be scoped to mcp-kubectl pod via podSelector."""
        sandbox_policy = None
        for doc in policies:
            if (
                doc
                and doc.get("kind") == "NetworkPolicy"
                and doc["metadata"].get("name") == "sandbox-egress"
            ):
                sandbox_policy = doc
                break

        assert sandbox_policy is not None, "Missing sandbox-egress NetworkPolicy"

        # Find the egress rule that includes port 8082
        rule_8082 = None
        for rule in sandbox_policy["spec"].get("egress", []):
            for port_spec in rule.get("ports", []):
                if port_spec.get("port") == 8082:
                    rule_8082 = rule
                    break

        assert rule_8082 is not None, "No egress rule with port 8082 found"
        assert "to" in rule_8082, (
            "Port 8082 egress rule must have a 'to' clause to scope traffic"
        )

        # Verify the to clause targets mcp-kubectl pods
        to_selectors = rule_8082["to"]
        labels = [
            selector.get("podSelector", {}).get("matchLabels", {})
            for selector in to_selectors
        ]
        assert any(l.get("app") == "mcp-kubectl" for l in labels), (
            "Port 8082 egress 'to' clause must target pods with app: mcp-kubectl"
        )


class TestWorkflowDefinition:
    """Validate the k8s-realcluster-workflow.yaml definition."""

    WORKFLOW_PATH = (
        ROOT / "examples" / "workflow-definitions" / "k8s-realcluster-workflow.yaml"
    )

    @pytest.fixture
    def workflow_data(self) -> dict:
        """Load the workflow YAML."""
        assert self.WORKFLOW_PATH.exists(), (
            f"Missing {self.WORKFLOW_PATH}"
        )
        with open(self.WORKFLOW_PATH) as f:
            return yaml.safe_load(f)

    def test_yaml_parses_and_validates(self, workflow_data: dict) -> None:
        """Workflow YAML validates against the Pydantic schema."""
        defn = WorkflowDefinition.model_validate(workflow_data)
        assert defn.metadata.get("name")
        assert len(defn.spec.steps) >= 1

    def test_temporal_validation_passes(self, workflow_data: dict) -> None:
        """Workflow passes temporal_validation checks."""
        errors = validate_definition(workflow_data)
        assert errors == [], f"Validation errors: {errors}"

    def test_uses_kubectl_mcp_server(self, workflow_data: dict) -> None:
        """At least one step must reference the kubectl MCP server."""
        steps = workflow_data["spec"]["steps"]
        has_kubectl = any(
            "kubectl" in (step.get("mcp_servers") or [])
            for step in steps
        )
        assert has_kubectl, "Workflow must reference 'kubectl' MCP server"

    def test_steps_have_tool_filtering(self, workflow_data: dict) -> None:
        """Steps with mcp_servers should have permissions.allowed_tools."""
        steps = workflow_data["spec"]["steps"]
        agent_steps_with_mcp = [
            s for s in steps
            if s.get("type") == "agent" and s.get("mcp_servers")
        ]
        assert len(agent_steps_with_mcp) > 0, "Must have agent steps with MCP servers"

        for step in agent_steps_with_mcp:
            permissions = step.get("permissions", {})
            assert permissions.get("allowed_tools"), (
                f"Step '{step['name']}' has mcp_servers but no permissions.allowed_tools — "
                "tool filtering is required for security"
            )

    def test_diagnose_step_has_read_only_tools(self, workflow_data: dict) -> None:
        """Diagnose step should only allow read-only MCP tools."""
        steps = workflow_data["spec"]["steps"]
        diagnose_steps = [
            s for s in steps
            if "diagnose" in s.get("name", "").lower() and s.get("permissions", {}).get("allowed_tools")
        ]
        assert len(diagnose_steps) > 0, "Must have a diagnose step with allowed_tools"

        for step in diagnose_steps:
            allowed = step["permissions"]["allowed_tools"]
            write_tools = [
                t for t in allowed
                if any(w in t for w in ["rollout", "scale", "apply", "delete", "create"])
            ]
            assert not write_tools, (
                f"Diagnose step '{step['name']}' allows write tools: {write_tools}"
            )

    def test_no_dead_fields(self, workflow_data: dict) -> None:
        """Steps must not use dead fields."""
        dead_fields = {"agent", "spawn"}
        for step in workflow_data["spec"]["steps"]:
            used_dead = dead_fields & set(step.keys())
            assert not used_dead, (
                f"Step '{step.get('name')}' uses dead fields: {used_dead}"
            )


class TestPermissionScopeToolFiltering:
    """Validate PermissionScope.effective_tools with MCP tool patterns."""

    def test_allowed_tools_filters_mcp_tools(self) -> None:
        """allowed_tools should filter MCP tools by mcp__<server>__<tool> pattern."""
        scope = PermissionScope(
            allowed_tools=[
                "mcp__kubectl__get_pods",
                "mcp__kubectl__get_events",
            ],
        )
        all_tools = [
            "mcp__kubectl__get_pods",
            "mcp__kubectl__get_events",
            "mcp__kubectl__rollout_restart",
            "mcp__kubectl__scale_deployment",
            "mcp__filesystem__read_file",
        ]
        result = scope.effective_tools(all_tools)
        assert result == ["mcp__kubectl__get_pods", "mcp__kubectl__get_events"]

    def test_denied_tools_excludes_write_tools(self) -> None:
        """denied_tools should exclude specific tools."""
        scope = PermissionScope(
            denied_tools=[
                "mcp__kubectl__rollout_restart",
                "mcp__kubectl__scale_deployment",
            ],
        )
        all_tools = [
            "mcp__kubectl__get_pods",
            "mcp__kubectl__rollout_restart",
            "mcp__kubectl__scale_deployment",
        ]
        result = scope.effective_tools(all_tools)
        assert result == ["mcp__kubectl__get_pods"]

    def test_allowed_and_denied_tools_combined(self) -> None:
        """Both allowed_tools and denied_tools applied together."""
        scope = PermissionScope(
            allowed_tools=[
                "mcp__kubectl__get_pods",
                "mcp__kubectl__get_events",
                "mcp__kubectl__rollout_restart",
            ],
            denied_tools=["mcp__kubectl__rollout_restart"],
        )
        all_tools = [
            "mcp__kubectl__get_pods",
            "mcp__kubectl__get_events",
            "mcp__kubectl__rollout_restart",
            "mcp__kubectl__scale_deployment",
        ]
        result = scope.effective_tools(all_tools)
        assert result == ["mcp__kubectl__get_pods", "mcp__kubectl__get_events"]


class TestMakefileTargets:
    """Validate Makefile has required targets for mcp-kubectl."""

    @pytest.fixture
    def makefile_content(self) -> str:
        """Read the Makefile."""
        return (ROOT / "Makefile").read_text()

    def test_build_mcp_kubectl_target(self, makefile_content: str) -> None:
        """Makefile must have a build-mcp-kubectl target."""
        assert "build-mcp-kubectl" in makefile_content, (
            "Makefile must define build-mcp-kubectl target"
        )

    def test_build_demo_includes_mcp_kubectl(self, makefile_content: str) -> None:
        """build-demo target should depend on build-mcp-kubectl."""
        # Find the build-demo line and check its dependencies
        lines = makefile_content.split("\n")
        for line in lines:
            if line.startswith("build-demo:"):
                assert "build-mcp-kubectl" in line, (
                    "build-demo target must depend on build-mcp-kubectl"
                )
                break

    def test_kind_up_deploys_mcp_kubectl(self, makefile_content: str) -> None:
        """kind-up target must deploy mcp-kubectl manifests."""
        assert "kind-mcp-kubectl" in makefile_content, (
            "kind-up must apply kind-mcp-kubectl.yaml"
        )

    def test_kind_up_loads_mcp_kubectl_image(self, makefile_content: str) -> None:
        """kind-up must load mcp-kubectl image into Kind cluster."""
        assert "mcp-kubectl" in makefile_content, (
            "kind-up must load mcp-kubectl image"
        )
