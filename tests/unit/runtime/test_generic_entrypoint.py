"""Unit tests for generic entrypoint."""

import os
import tempfile

import pytest
import yaml

from agents.runtime.generic_entrypoint import build_app, load_definition, load_registry

MINIMAL_AGENT_YAML = {
    "apiVersion": "lightspeed.redhat.com/v1alpha1",
    "kind": "AgentDefinition",
    "metadata": {"name": "test-agent"},
    "spec": {
        "instructions": "You are a test agent.",
        "output_type": "str",
        "tools": {
            "module": "examples.agents.diagnostic.tools",
            "functions": ["list_hosts"],
        },
        "lifecycle": {"type": "request-response"},
    },
}


class TestLoadDefinition:
    """Tests for load_definition."""

    def test_loads_valid_yaml(self) -> None:
        """Test loading a valid agent.yaml."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(MINIMAL_AGENT_YAML, f)
            path = f.name
        try:
            defn = load_definition(path)
            assert defn.metadata["name"] == "test-agent"
        finally:
            os.unlink(path)

    def test_missing_file_raises(self) -> None:
        """Test that a missing file raises RuntimeError."""
        with pytest.raises(RuntimeError, match="not found"):
            load_definition("/nonexistent/agent.yaml")


class TestLoadRegistry:
    """Tests for load_registry."""

    def test_loads_valid_registry(self) -> None:
        """Test loading a valid registry.yaml."""
        registry_data = {
            "agents": [
                {"name": "diagnostic-agent", "endpoint": "http://diag:8080"},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(registry_data, f)
            path = f.name
        try:
            registry = load_registry(path)
            assert registry.get_endpoint("diagnostic-agent") == "http://diag:8080"
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self) -> None:
        """Test that a missing file returns empty registry."""
        registry = load_registry("/nonexistent/registry.yaml")
        assert registry.list_agents() == []


class TestBuildApp:
    """Tests for build_app."""

    def test_builds_request_response_app(self) -> None:
        """Test building a request-response app from YAML."""
        from examples.agents.diagnostic.cluster_state import init_scenario

        init_scenario("healthy")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(MINIMAL_AGENT_YAML, f)
            path = f.name
        try:
            app = build_app(definition_path=path)
            assert app is not None
            assert "test-agent" in app.title
        finally:
            os.unlink(path)
