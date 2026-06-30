"""Validate the workflow YAML embedded in DEMO.md against the schema.

Extracts the diagnostic-workflow.yaml content from DEMO.md and validates it.
If the DEMO changes and the YAML becomes invalid, this test catches it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from cloud_agents.workflow.definition import WorkflowDefinition
from cloud_agents.workflow.temporal_validation import validate_definition

DEMO_PATH = Path(__file__).parents[3] / "docs" / "DEMO.md"


def _extract_workflow_yaml_from_demo() -> str | None:
    """Extract the diagnostic-workflow.yaml block from DEMO.md."""
    if not DEMO_PATH.exists():
        return None
    content = DEMO_PATH.read_text()
    match = re.search(
        r"Save this as `diagnostic-workflow\.yaml`:\s*\n```yaml\n(.*?)```",
        content,
        re.DOTALL,
    )
    return match.group(1) if match else None


class TestDemoYaml:
    """Validate DEMO.md's embedded workflow YAML."""

    @pytest.fixture
    def demo_yaml(self) -> dict:
        """Extract and parse the DEMO workflow YAML."""
        raw = _extract_workflow_yaml_from_demo()
        if raw is None:
            pytest.skip("Could not extract workflow YAML from DEMO.md")
        return yaml.safe_load(raw)

    def test_demo_yaml_is_valid_yaml(self, demo_yaml: dict) -> None:
        """DEMO.md workflow YAML parses as valid YAML."""
        assert demo_yaml is not None
        assert demo_yaml.get("kind") == "AgentWorkflow"

    def test_demo_yaml_validates_against_model(self, demo_yaml: dict) -> None:
        """DEMO.md workflow YAML validates against WorkflowDefinition."""
        defn = WorkflowDefinition.model_validate(demo_yaml)
        assert defn.metadata.get("name") == "diagnose-production"
        assert len(defn.spec.steps) >= 1

    def test_demo_yaml_passes_temporal_validation(self, demo_yaml: dict) -> None:
        """DEMO.md workflow YAML passes temporal_validation checks."""
        errors = validate_definition(demo_yaml)
        assert errors == [], f"Validation errors: {errors}"

    def test_demo_yaml_no_dead_fields(self, demo_yaml: dict) -> None:
        """DEMO.md workflow YAML doesn't use dead fields."""
        dead_fields = {"agent", "spawn"}
        for step in demo_yaml["spec"]["steps"]:
            used_dead = dead_fields & set(step.keys())
            assert not used_dead, f"Step '{step.get('name')}' uses dead fields: {used_dead}"

    def test_demo_yaml_matches_example_file(self, demo_yaml: dict) -> None:
        """DEMO.md YAML matches the standalone example file."""
        example_path = Path(__file__).parents[3] / "examples" / "definitions" / "diagnostic-workflow.yaml"
        if not example_path.exists():
            pytest.skip("diagnostic-workflow.yaml example file not found")
        with open(example_path) as f:
            example = yaml.safe_load(f)
        assert demo_yaml["metadata"]["name"] == example["metadata"]["name"]
        assert len(demo_yaml["spec"]["steps"]) == len(example["spec"]["steps"])
        for demo_step, example_step in zip(demo_yaml["spec"]["steps"], example["spec"]["steps"]):
            assert demo_step["name"] == example_step["name"]
            assert demo_step["type"] == example_step["type"]
            assert demo_step["output_key"] == example_step["output_key"]
