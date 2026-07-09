"""Verify committed JSON Schema matches Pydantic models.

Fails if someone changes the models without regenerating the schema.
Fix: uv run python schema/generate.py
"""

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).parents[3] / "schema" / "workflow-definition.schema.json"


def test_schema_matches_models():
    """Committed schema must match generate.py output."""
    # Import here so the test fails clearly if the generator breaks
    from schema.generate import generate

    assert SCHEMA_PATH.exists(), (
        f"{SCHEMA_PATH} missing. Run: uv run python schema/generate.py"
    )
    committed = SCHEMA_PATH.read_text()
    current = generate()
    assert committed == current, (
        "Schema is stale. Run: uv run python schema/generate.py"
    )
