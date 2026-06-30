"""Unit tests for WorkflowDefinition model."""

import pytest
import yaml
from pydantic import ValidationError

from agents.workflow.definition import WorkflowDefinition

MINIMAL_WORKFLOW = """
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentWorkflow
metadata:
  name: test-workflow
spec:
  steps:
    - name: step1
      type: agent
      agent: diagnostic-agent
      prompt: "Do something"
      output_key: result1
"""

FULL_WORKFLOW = """
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentWorkflow
metadata:
  name: cluster-rca
  description: "4-step RCA workflow"
spec:
  input_prompt: "Investigate the cluster"
  steps:
    - name: diagnose
      type: agent
      agent: diagnostic-agent
      prompt: "Diagnose all issues"
      output_key: diagnosis

    - name: recommend
      type: agent
      agent: diagnostic-agent
      prompt: "Based on {{ steps.diagnose.output.summary }}, recommend fixes"
      output_key: plan

    - name: approve
      type: human-approval
      message: "Approve the plan?"
      output_key: approval
      timeout_seconds: 1800

    - name: execute
      type: agent
      agent: diagnostic-agent
      prompt: "Execute the plan"
      output_key: execution
      condition: "steps.approve.approved == true"
"""


class TestWorkflowDefinitionParsing:
    """Tests for parsing WorkflowDefinition from YAML."""

    def test_minimal_parses(self) -> None:
        """Test that a minimal workflow YAML parses."""
        data = yaml.safe_load(MINIMAL_WORKFLOW)
        defn = WorkflowDefinition.model_validate(data)
        assert defn.metadata["name"] == "test-workflow"
        assert len(defn.spec.steps) == 1
        assert defn.spec.steps[0].type == "agent"

    def test_full_parses(self) -> None:
        """Test that a full workflow YAML parses all fields."""
        data = yaml.safe_load(FULL_WORKFLOW)
        defn = WorkflowDefinition.model_validate(data)
        assert defn.metadata["name"] == "cluster-rca"
        assert len(defn.spec.steps) == 4
        assert defn.spec.steps[2].type == "human-approval"
        assert defn.spec.steps[2].timeout_seconds == 1800
        assert defn.spec.steps[3].condition == "steps.approve.approved == true"

    def test_json_round_trip(self) -> None:
        """Test serialization round-trip."""
        data = yaml.safe_load(FULL_WORKFLOW)
        defn = WorkflowDefinition.model_validate(data)
        json_str = defn.model_dump_json()
        restored = WorkflowDefinition.model_validate_json(json_str)
        assert restored.metadata["name"] == "cluster-rca"
        assert len(restored.spec.steps) == 4


class TestWorkflowDefinitionValidation:
    """Tests for validation constraints."""

    def test_invalid_kind_rejected(self) -> None:
        """Test wrong kind is rejected."""
        data = yaml.safe_load(MINIMAL_WORKFLOW)
        data["kind"] = "WrongKind"
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(data)

    def test_empty_steps_rejected(self) -> None:
        """Test empty steps list is rejected."""
        data = yaml.safe_load(MINIMAL_WORKFLOW)
        data["spec"]["steps"] = []
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(data)

    def test_invalid_step_type_rejected(self) -> None:
        """Test invalid step type is rejected."""
        data = yaml.safe_load(MINIMAL_WORKFLOW)
        data["spec"]["steps"][0]["type"] = "unknown"
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(data)

    def test_valid_step_types(self) -> None:
        """Test all valid step types are accepted."""
        for stype in ("agent", "human-approval"):
            data = yaml.safe_load(MINIMAL_WORKFLOW)
            data["spec"]["steps"][0]["type"] = stype
            defn = WorkflowDefinition.model_validate(data)
            assert defn.spec.steps[0].type == stype

    def test_missing_output_key_rejected(self) -> None:
        """Test missing output_key is rejected."""
        data = yaml.safe_load(MINIMAL_WORKFLOW)
        del data["spec"]["steps"][0]["output_key"]
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(data)
