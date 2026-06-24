"""Unit tests for workflow runner entrypoint."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from agents.workflow.entrypoint import _load_workflow, _load_registry, build_workflow_app


MINIMAL_WORKFLOW = {
    "apiVersion": "v1",
    "kind": "AgentWorkflow",
    "metadata": {"name": "test"},
    "spec": {
        "steps": [
            {"name": "s1", "type": "agent", "agent": "diag",
             "prompt": "test", "output_key": "r1", "spawn": "pre-deployed"},
        ],
    },
}

REGISTRY = {
    "agents": [
        {"name": "diag", "endpoint": "http://diag:8080"},
    ],
}


class TestLoadWorkflow:
    """Tests for _load_workflow."""

    def test_loads_valid_yaml(self) -> None:
        """Test loading a valid workflow.yaml."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(MINIMAL_WORKFLOW, f)
            path = f.name
        try:
            defn = _load_workflow(path)
            assert defn.metadata["name"] == "test"
        finally:
            os.unlink(path)

    def test_missing_file_raises(self) -> None:
        """Test that a missing file raises RuntimeError."""
        with pytest.raises(RuntimeError, match="not found"):
            _load_workflow("/nonexistent/workflow.yaml")


class TestLoadRegistry:
    """Tests for _load_registry."""

    def test_loads_valid_registry(self) -> None:
        """Test loading a valid registry.yaml."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(REGISTRY, f)
            path = f.name
        try:
            registry = _load_registry(path)
            assert registry.get_endpoint("diag") == "http://diag:8080"
        finally:
            os.unlink(path)

    def test_missing_file_raises(self) -> None:
        """Test that a missing registry file raises RuntimeError."""
        with pytest.raises(RuntimeError, match="not found"):
            _load_registry("/nonexistent/registry.yaml")


class TestBuildWorkflowApp:
    """Tests for build_workflow_app."""

    def test_builds_app(self) -> None:
        """Test building a workflow app from YAML files."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as wf:
            yaml.dump(MINIMAL_WORKFLOW, wf)
            wf_path = wf.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as rf:
            yaml.dump(REGISTRY, rf)
            rf_path = rf.name
        try:
            app = build_workflow_app(workflow_path=wf_path, registry_path=rf_path)
            assert app is not None
            assert "test" in app.title
        finally:
            os.unlink(wf_path)
            os.unlink(rf_path)

    @pytest.mark.asyncio
    async def test_postgres_persistence_initialized_on_startup(self) -> None:
        """Test that PostgresPersistence.initialize() is called during lifespan."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as wf:
            yaml.dump(MINIMAL_WORKFLOW, wf)
            wf_path = wf.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as rf:
            yaml.dump(REGISTRY, rf)
            rf_path = rf.name
        try:
            mock_persistence = MagicMock()
            mock_persistence.initialize = AsyncMock()
            mock_persistence.save = AsyncMock()
            mock_persistence.load = AsyncMock(return_value=None)

            with patch("agents.workflow.entrypoint._create_persistence", return_value=mock_persistence):
                app = build_workflow_app(workflow_path=wf_path, registry_path=rf_path)

            assert app.router.lifespan_context is not None
            async with app.router.lifespan_context(app):
                pass

            mock_persistence.initialize.assert_awaited_once()
        finally:
            os.unlink(wf_path)
            os.unlink(rf_path)
