"""Validate all example workflow YAML files against the Pydantic schema.

Catches drift between documentation examples and the actual schema.
If a YAML file in examples/agents/definitions/ doesn't validate,
the example is wrong and must be fixed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from cloud_agents.workflow.definition import WorkflowDefinition
from cloud_agents.workflow.temporal_validation import validate_definition

EXAMPLES_DIR = Path(__file__).parents[3] / "examples" / "workflow-definitions"


def _workflow_yamls() -> list[Path]:
    """Collect all workflow YAML files from the examples directory."""
    if not EXAMPLES_DIR.exists():
        return []
    return [f for f in EXAMPLES_DIR.glob("*-workflow*.yaml")]


@pytest.mark.parametrize(
    "yaml_path",
    _workflow_yamls(),
    ids=lambda p: p.name,
)
class TestExampleWorkflowDefinitions:
    """Validate example workflow definitions against the schema."""

    def test_yaml_parses(self, yaml_path: Path) -> None:
        """Example YAML is valid YAML."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert data is not None
        assert data.get("apiVersion") == "v1"
        assert data.get("kind") == "AgentWorkflow"

    def test_pydantic_model_validates(self, yaml_path: Path) -> None:
        """Example YAML validates against WorkflowDefinition Pydantic model."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        defn = WorkflowDefinition.model_validate(data)
        assert defn.metadata.get("name")
        assert len(defn.spec.steps) >= 1

    def test_temporal_validation_passes(self, yaml_path: Path) -> None:
        """Example YAML passes temporal_validation checks."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        errors = validate_definition(data)
        assert errors == [], f"Validation errors in {yaml_path.name}: {errors}"

    def test_steps_have_required_fields(self, yaml_path: Path) -> None:
        """Every step has name, type, and output_key."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        for step in data["spec"]["steps"]:
            assert "name" in step, f"Step missing 'name' in {yaml_path.name}"
            assert "type" in step, f"Step '{step.get('name')}' missing 'type' in {yaml_path.name}"
            assert "output_key" in step, f"Step '{step.get('name')}' missing 'output_key' in {yaml_path.name}"

    def test_no_dead_fields(self, yaml_path: Path) -> None:
        """Steps don't use fields that the Temporal workflow ignores."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        dead_fields = {"agent", "spawn"}
        for step in data["spec"]["steps"]:
            used_dead = dead_fields & set(step.keys())
            assert not used_dead, (
                f"Step '{step.get('name')}' in {yaml_path.name} uses dead fields "
                f"not read by temporal_workflow.py: {used_dead}"
            )
