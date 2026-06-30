"""Unit tests for AgentDefinition YAML schema model."""

import pytest
import yaml
from pydantic import ValidationError

from agents.definition import (
    AgentDefinition,
    LifecycleSpec,
    ToolsSpec,
)

MINIMAL_YAML = """
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: test-agent
spec:
  instructions: "You are a test agent."
  output_type: str
  tools:
    module: test_tools
    functions:
      - my_tool
  lifecycle:
    type: request-response
"""

FULL_YAML = """
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: diagnostic-agent
spec:
  instructions: "You are a diagnostic agent."
  output_type: DiagnosticReport
  output_type_module: diagnostic_tools
  retries: 3
  defer_model_check: true
  tools:
    module: diagnostic_tools
    functions:
      - list_hosts
      - check_host
  skills:
    directories:
      - /app/skills
  lifecycle:
    type: periodic-loop
    interval_seconds: 300
    dispatch_to: diagnostic-agent
    on_dispatch_success:
      module: monitoring_tools
      function: mark_hosts_healthy
  output_validator:
    module: diagnostic_tools
    function: verify_all_fixed
  resources:
    max_tokens_per_run: 50000
    timeout_seconds: 600
"""


class TestAgentDefinitionParsing:
    """Tests for parsing AgentDefinition from YAML."""

    def test_minimal_yaml_parses(self) -> None:
        """Test that a minimal YAML definition parses correctly."""
        data = yaml.safe_load(MINIMAL_YAML)
        defn = AgentDefinition.model_validate(data)
        assert defn.metadata["name"] == "test-agent"
        assert defn.spec.output_type == "str"
        assert defn.spec.retries == 1

    def test_full_yaml_parses(self) -> None:
        """Test that a full YAML definition parses all fields."""
        data = yaml.safe_load(FULL_YAML)
        defn = AgentDefinition.model_validate(data)
        assert defn.metadata["name"] == "diagnostic-agent"
        assert defn.spec.output_type == "DiagnosticReport"
        assert defn.spec.output_type_module == "diagnostic_tools"
        assert defn.spec.retries == 3
        assert len(defn.spec.tools.functions) == 2
        assert defn.spec.lifecycle.type == "periodic-loop"
        assert defn.spec.lifecycle.interval_seconds == 300
        assert defn.spec.lifecycle.dispatch_to == "diagnostic-agent"
        assert defn.spec.lifecycle.on_dispatch_success.function == "mark_hosts_healthy"
        assert defn.spec.output_validator.function == "verify_all_fixed"
        assert defn.spec.skills.directories == ["/app/skills"]

    def test_json_round_trip(self) -> None:
        """Test serialization round-trip."""
        data = yaml.safe_load(FULL_YAML)
        defn = AgentDefinition.model_validate(data)
        json_str = defn.model_dump_json()
        restored = AgentDefinition.model_validate_json(json_str)
        assert restored.metadata["name"] == "diagnostic-agent"
        assert restored.spec.tools.module == "diagnostic_tools"


class TestAgentDefinitionValidation:
    """Tests for validation constraints."""

    def test_invalid_kind_rejected(self) -> None:
        """Test that wrong kind is rejected."""
        data = yaml.safe_load(MINIMAL_YAML)
        data["kind"] = "WrongKind"
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(data)

    def test_missing_instructions_rejected(self) -> None:
        """Test that missing instructions is rejected."""
        data = yaml.safe_load(MINIMAL_YAML)
        del data["spec"]["instructions"]
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(data)

    def test_missing_tools_rejected(self) -> None:
        """Test that missing tools is rejected."""
        data = yaml.safe_load(MINIMAL_YAML)
        del data["spec"]["tools"]
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(data)

    def test_empty_functions_list_rejected(self) -> None:
        """Test that empty tools.functions is rejected."""
        data = yaml.safe_load(MINIMAL_YAML)
        data["spec"]["tools"]["functions"] = []
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(data)

    def test_invalid_lifecycle_type_rejected(self) -> None:
        """Test that invalid lifecycle type is rejected."""
        data = yaml.safe_load(MINIMAL_YAML)
        data["spec"]["lifecycle"]["type"] = "invalid"
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(data)

    def test_valid_lifecycle_types_accepted(self) -> None:
        """Test that all valid lifecycle types are accepted."""
        for ltype in ("request-response", "periodic-loop"):
            data = yaml.safe_load(MINIMAL_YAML)
            data["spec"]["lifecycle"]["type"] = ltype
            defn = AgentDefinition.model_validate(data)
            assert defn.spec.lifecycle.type == ltype


class TestToolsSpec:
    """Tests for ToolsSpec validation."""

    def test_valid_tools_spec(self) -> None:
        """Test creating a valid ToolsSpec."""
        spec = ToolsSpec(module="my_tools", functions=["fn1", "fn2"])
        assert spec.module == "my_tools"
        assert len(spec.functions) == 2

    def test_empty_module_rejected(self) -> None:
        """Test that empty module name is rejected."""
        with pytest.raises(ValidationError):
            ToolsSpec(module="", functions=["fn"])


class TestLifecycleSpec:
    """Tests for LifecycleSpec validation."""

    def test_request_response_defaults(self) -> None:
        """Test request-response lifecycle defaults."""
        spec = LifecycleSpec(type="request-response")
        assert spec.interval_seconds == 300
        assert spec.dispatch_to is None

    def test_periodic_loop_with_dispatch(self) -> None:
        """Test periodic-loop with dispatch configuration."""
        spec = LifecycleSpec(
            type="periodic-loop",
            interval_seconds=60,
            dispatch_to="diagnostic-agent",
        )
        assert spec.type == "periodic-loop"
        assert spec.dispatch_to == "diagnostic-agent"
