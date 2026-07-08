"""Tests for CVE patch workflow demo with mock RHDH catalog MCP server.

Validates:
1. Mock catalog data structure (catalog-data.json)
2. Mock MCP server Containerfile follows established patterns
3. CVE patch workflow YAML validates against the schema
4. Docker Compose overlay is correct
5. Makefile targets exist for build and demo
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cloud_agents.workflow.definition import WorkflowDefinition
from cloud_agents.workflow.temporal_validation import validate_definition

ROOT = Path(__file__).parents[2]


class TestCatalogData:
    """Validate the mock RHDH catalog data structure."""

    DATA_PATH = ROOT / "deploy" / "mcp-rhdh-mock" / "catalog-data.json"

    @pytest.fixture
    def catalog_data(self) -> dict:
        """Load the catalog data JSON."""
        assert self.DATA_PATH.exists(), (
            f"Missing {self.DATA_PATH} — mock catalog data file is required"
        )
        with open(self.DATA_PATH) as f:
            return json.load(f)

    def test_has_components(self, catalog_data: dict) -> None:
        """Catalog data must have a 'components' list."""
        assert "components" in catalog_data
        assert isinstance(catalog_data["components"], list)
        assert len(catalog_data["components"]) >= 2

    def test_component_has_required_fields(self, catalog_data: dict) -> None:
        """Each component must have name, repo, owner, tech_stack, dependencies."""
        required_fields = {"name", "repo", "owner", "tech_stack", "dependencies"}
        for component in catalog_data["components"]:
            missing = required_fields - set(component.keys())
            assert not missing, (
                f"Component '{component.get('name', '?')}' missing fields: {missing}"
            )

    def test_dependencies_have_required_fields(self, catalog_data: dict) -> None:
        """Each dependency must have name, version, cve, patched fields."""
        required_fields = {"name", "version", "cve", "patched"}
        for component in catalog_data["components"]:
            for dep in component["dependencies"]:
                missing = required_fields - set(dep.keys())
                assert not missing, (
                    f"Dependency '{dep.get('name', '?')}' in "
                    f"'{component['name']}' missing fields: {missing}"
                )

    def test_has_vulnerable_components(self, catalog_data: dict) -> None:
        """At least two components must have CVEs for demo purposes."""
        vulnerable_count = 0
        for component in catalog_data["components"]:
            has_cve = any(dep.get("cve") for dep in component["dependencies"])
            if has_cve:
                vulnerable_count += 1
        assert vulnerable_count >= 2, (
            f"Need at least 2 vulnerable components for demo, found {vulnerable_count}"
        )

    def test_payment_gateway_has_spring_boot_cve(self, catalog_data: dict) -> None:
        """payment-gateway must have a Spring Boot CVE."""
        pg = next(
            (c for c in catalog_data["components"] if c["name"] == "payment-gateway"),
            None,
        )
        assert pg is not None, "Missing payment-gateway component"
        spring_deps = [d for d in pg["dependencies"] if d["name"] == "spring-boot"]
        assert len(spring_deps) == 1, "payment-gateway must have spring-boot dependency"
        assert spring_deps[0]["cve"] is not None, "spring-boot must have a CVE"
        assert spring_deps[0]["patched"] is not None, "spring-boot must have a patched version"

    def test_user_service_has_pydantic_cve(self, catalog_data: dict) -> None:
        """user-service must have a pydantic CVE."""
        us = next(
            (c for c in catalog_data["components"] if c["name"] == "user-service"),
            None,
        )
        assert us is not None, "Missing user-service component"
        pydantic_deps = [d for d in us["dependencies"] if d["name"] == "pydantic"]
        assert len(pydantic_deps) == 1, "user-service must have pydantic dependency"
        assert pydantic_deps[0]["cve"] is not None, "pydantic must have a CVE"
        assert pydantic_deps[0]["patched"] is not None, "pydantic must have a patched version"

    def test_has_cve_database(self, catalog_data: dict) -> None:
        """Catalog data must have a 'cve_database' section for CVE details."""
        assert "cve_database" in catalog_data
        assert isinstance(catalog_data["cve_database"], dict)
        assert len(catalog_data["cve_database"]) >= 2

    def test_cve_database_entries_have_required_fields(self, catalog_data: dict) -> None:
        """Each CVE database entry must have description, severity, affected_versions, patched_version."""
        required_fields = {"description", "severity", "affected_versions", "patched_version"}
        for cve_id, cve_data in catalog_data["cve_database"].items():
            missing = required_fields - set(cve_data.keys())
            assert not missing, (
                f"CVE '{cve_id}' missing fields: {missing}"
            )

    def test_cve_ids_match_component_references(self, catalog_data: dict) -> None:
        """CVE IDs referenced in components must exist in the cve_database."""
        referenced_cves = set()
        for component in catalog_data["components"]:
            for dep in component["dependencies"]:
                if dep.get("cve"):
                    referenced_cves.add(dep["cve"])

        db_cves = set(catalog_data["cve_database"].keys())
        missing = referenced_cves - db_cves
        assert not missing, (
            f"CVEs referenced in components but missing from cve_database: {missing}"
        )


class TestMockServerContainerfile:
    """Validate the mcp-rhdh-mock Containerfile."""

    CONTAINERFILE = ROOT / "deploy" / "mcp-rhdh-mock" / "Containerfile"

    def test_containerfile_exists(self) -> None:
        """Containerfile for mcp-rhdh-mock must exist."""
        assert self.CONTAINERFILE.exists(), (
            f"Missing {self.CONTAINERFILE}"
        )

    def test_containerfile_uses_python_base(self) -> None:
        """Containerfile should use a Python base image."""
        content = self.CONTAINERFILE.read_text()
        assert "FROM" in content
        assert "python" in content.lower(), "Should use a Python base image"

    def test_containerfile_installs_fastmcp(self) -> None:
        """Containerfile must install fastmcp (via requirements.txt)."""
        content = self.CONTAINERFILE.read_text()
        # fastmcp is installed via pip from requirements.txt
        assert "requirements.txt" in content, (
            "Must install Python deps from requirements.txt (which includes fastmcp)"
        )

    def test_containerfile_installs_supergateway(self) -> None:
        """Containerfile must install supergateway for streamable HTTP."""
        content = self.CONTAINERFILE.read_text()
        assert "supergateway" in content, "Must install supergateway"

    def test_containerfile_exposes_port_8083(self) -> None:
        """Containerfile must expose port 8083."""
        content = self.CONTAINERFILE.read_text()
        assert "8083" in content, "Must expose port 8083"

    def test_containerfile_copies_server_and_data(self) -> None:
        """Containerfile must copy server.py and catalog-data.json."""
        content = self.CONTAINERFILE.read_text()
        assert "server.py" in content, "Must copy server.py"
        assert "catalog-data.json" in content, "Must copy catalog-data.json"


class TestMockServerRequirements:
    """Validate the requirements.txt for the mock server."""

    REQUIREMENTS = ROOT / "deploy" / "mcp-rhdh-mock" / "requirements.txt"

    def test_requirements_exists(self) -> None:
        """requirements.txt must exist."""
        assert self.REQUIREMENTS.exists()

    def test_requires_fastmcp(self) -> None:
        """Must require fastmcp."""
        content = self.REQUIREMENTS.read_text()
        assert "fastmcp" in content.lower()


class TestMockServerScript:
    """Validate the mock MCP server script exists and has the right tools."""

    SERVER_PATH = ROOT / "deploy" / "mcp-rhdh-mock" / "server.py"

    def test_server_exists(self) -> None:
        """server.py must exist."""
        assert self.SERVER_PATH.exists()

    def test_server_defines_list_components_tool(self) -> None:
        """server.py must define a list_components tool."""
        content = self.SERVER_PATH.read_text()
        assert "list_components" in content

    def test_server_defines_get_component_tool(self) -> None:
        """server.py must define a get_component tool."""
        content = self.SERVER_PATH.read_text()
        assert "get_component" in content

    def test_server_defines_check_vulnerabilities_tool(self) -> None:
        """server.py must define a check_vulnerabilities tool."""
        content = self.SERVER_PATH.read_text()
        assert "check_vulnerabilities" in content

    def test_server_defines_get_cve_details_tool(self) -> None:
        """server.py must define a get_cve_details tool."""
        content = self.SERVER_PATH.read_text()
        assert "get_cve_details" in content

    def test_server_loads_catalog_data(self) -> None:
        """server.py must load catalog-data.json."""
        content = self.SERVER_PATH.read_text()
        assert "catalog-data.json" in content


class TestCvePatchWorkflow:
    """Validate the CVE patch workflow YAML definition."""

    WORKFLOW_PATH = (
        ROOT / "examples" / "workflow-definitions" / "cve-patch-workflow.yaml"
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
        assert defn.metadata.get("name") == "cve-patch"
        assert len(defn.spec.steps) == 4

    def test_temporal_validation_passes(self, workflow_data: dict) -> None:
        """Workflow passes temporal_validation checks."""
        errors = validate_definition(workflow_data)
        assert errors == [], f"Validation errors: {errors}"

    def test_has_four_steps(self, workflow_data: dict) -> None:
        """Workflow must have 4 steps: scan, approve, patch, verify."""
        steps = workflow_data["spec"]["steps"]
        assert len(steps) == 4
        step_names = [s["name"] for s in steps]
        assert "scan-vulnerabilities" in step_names
        assert "approve-patch" in step_names
        assert "apply-patches" in step_names
        assert "verify-ci" in step_names

    def test_scan_step_uses_rhdh_catalog(self, workflow_data: dict) -> None:
        """Scan step must reference the rhdh-catalog MCP server."""
        scan_step = next(
            s for s in workflow_data["spec"]["steps"]
            if s["name"] == "scan-vulnerabilities"
        )
        assert "rhdh-catalog" in scan_step.get("mcp_servers", [])

    def test_scan_step_has_output_schema(self, workflow_data: dict) -> None:
        """Scan step must have an output_schema."""
        scan_step = next(
            s for s in workflow_data["spec"]["steps"]
            if s["name"] == "scan-vulnerabilities"
        )
        schema = scan_step.get("output_schema")
        assert schema is not None
        assert schema.get("type") == "object"
        assert "affected_components" in schema.get("properties", {})
        assert "cve_count" in schema.get("properties", {})

    def test_approve_step_is_human_approval(self, workflow_data: dict) -> None:
        """Approve step must be type human-approval."""
        approve_step = next(
            s for s in workflow_data["spec"]["steps"]
            if s["name"] == "approve-patch"
        )
        assert approve_step["type"] == "human-approval"
        assert approve_step.get("risk_level") in ("high", "critical")

    def test_patch_step_conditional_on_approval(self, workflow_data: dict) -> None:
        """Patch step must be conditional on approval."""
        patch_step = next(
            s for s in workflow_data["spec"]["steps"]
            if s["name"] == "apply-patches"
        )
        assert patch_step.get("condition") is not None
        assert "approval" in patch_step["condition"]
        assert "approved" in patch_step["condition"]

    def test_verify_step_conditional_on_patches(self, workflow_data: dict) -> None:
        """Verify step must be conditional on patch completion."""
        verify_step = next(
            s for s in workflow_data["spec"]["steps"]
            if s["name"] == "verify-ci"
        )
        assert verify_step.get("condition") is not None
        assert "patches" in verify_step["condition"]

    def test_agent_steps_have_tool_filtering(self, workflow_data: dict) -> None:
        """Agent steps with mcp_servers must have permissions.allowed_tools."""
        steps = workflow_data["spec"]["steps"]
        agent_steps_with_mcp = [
            s for s in steps
            if s.get("type") == "agent" and s.get("mcp_servers")
        ]
        assert len(agent_steps_with_mcp) >= 1

        for step in agent_steps_with_mcp:
            permissions = step.get("permissions", {})
            assert permissions.get("allowed_tools"), (
                f"Step '{step['name']}' has mcp_servers but no "
                "permissions.allowed_tools — tool filtering required"
            )

    def test_scan_step_has_read_only_tools(self, workflow_data: dict) -> None:
        """Scan step should only have read-only catalog tools."""
        scan_step = next(
            s for s in workflow_data["spec"]["steps"]
            if s["name"] == "scan-vulnerabilities"
        )
        allowed = scan_step.get("permissions", {}).get("allowed_tools", [])
        assert len(allowed) > 0
        write_tools = [
            t for t in allowed
            if any(w in t for w in ["create", "delete", "update", "fork", "patch"])
        ]
        assert not write_tools, (
            f"Scan step allows write tools: {write_tools}"
        )

    def test_no_dead_fields(self, workflow_data: dict) -> None:
        """Steps must not use dead fields."""
        dead_fields = {"agent", "spawn"}
        for step in workflow_data["spec"]["steps"]:
            used_dead = dead_fields & set(step.keys())
            assert not used_dead, (
                f"Step '{step.get('name')}' uses dead fields: {used_dead}"
            )


class TestCveDemoComposeOverlay:
    """Validate the CVE demo Docker Compose overlay."""

    COMPOSE_PATH = ROOT / "deploy" / "podman" / "docker-compose.cve-demo.yaml"

    @pytest.fixture
    def compose_data(self) -> dict:
        """Load the compose overlay YAML."""
        assert self.COMPOSE_PATH.exists(), (
            f"Missing {self.COMPOSE_PATH}"
        )
        with open(self.COMPOSE_PATH) as f:
            return yaml.safe_load(f)

    def test_has_mcp_rhdh_mock_service(self, compose_data: dict) -> None:
        """Overlay must define the mcp-rhdh-mock service."""
        services = compose_data.get("services", {})
        assert "mcp-rhdh-mock" in services

    def test_mcp_rhdh_mock_uses_correct_image(self, compose_data: dict) -> None:
        """mcp-rhdh-mock service must use the correct image."""
        svc = compose_data["services"]["mcp-rhdh-mock"]
        assert svc.get("image") == "localhost/mcp-rhdh-mock:latest"

    def test_mcp_rhdh_mock_exposes_port_8083(self, compose_data: dict) -> None:
        """mcp-rhdh-mock service must expose port 8083."""
        svc = compose_data["services"]["mcp-rhdh-mock"]
        ports = svc.get("ports", [])
        assert any("8083" in str(p) for p in ports), (
            f"mcp-rhdh-mock must expose port 8083, got: {ports}"
        )


class TestMakefileTargets:
    """Validate Makefile has required targets for CVE demo."""

    @pytest.fixture
    def makefile_content(self) -> str:
        """Read the Makefile."""
        return (ROOT / "Makefile").read_text()

    def test_build_mcp_rhdh_mock_target(self, makefile_content: str) -> None:
        """Makefile must have a build-mcp-rhdh-mock target."""
        assert "build-mcp-rhdh-mock" in makefile_content, (
            "Makefile must define build-mcp-rhdh-mock target"
        )

    def test_cve_demo_up_target(self, makefile_content: str) -> None:
        """Makefile must have a cve-demo-up target."""
        assert "cve-demo-up" in makefile_content, (
            "Makefile must define cve-demo-up target"
        )
