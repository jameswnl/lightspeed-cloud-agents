"""Verify committed JSON Schema matches Pydantic models.

Fails if someone changes the models without regenerating the schema.
Fix: uv run python schema/generate.py
"""

import json
from pathlib import Path

from cloud_agents.workflow.definition import WorkflowDefinition

SCHEMA_PATH = Path(__file__).parents[3] / "schema" / "workflow-definition.schema.json"


def test_schema_matches_models():
    """Committed schema must match WorkflowDefinition.model_json_schema()."""
    assert SCHEMA_PATH.exists(), (
        f"{SCHEMA_PATH} missing. Run: uv run python schema/generate.py"
    )
    committed = json.loads(SCHEMA_PATH.read_text())
    current = WorkflowDefinition.model_json_schema()
    assert committed == current, (
        "Schema is stale. Run: uv run python schema/generate.py"
    )
