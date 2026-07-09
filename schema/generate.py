#!/usr/bin/env python3
"""Generate JSON Schema from Pydantic workflow definition models.

Usage:
    python schema/generate.py              # write to schema/workflow-definition.schema.json
    python schema/generate.py --check      # exit 1 if committed schema is stale
"""

import argparse
import json
import sys
from pathlib import Path

from cloud_agents.workflow.definition import WorkflowDefinition

SCHEMA_PATH = Path(__file__).parent / "workflow-definition.schema.json"

DEAD_STEP_FIELDS = {"spawn", "agent", "spawn_config"}


def _strip_dead_fields(schema: dict) -> dict:
    """Remove dead fields from WorkflowStepSpec in the generated schema."""
    step_spec = schema.get("$defs", {}).get("WorkflowStepSpec", {})
    props = step_spec.get("properties", {})
    for field in DEAD_STEP_FIELDS:
        props.pop(field, None)
    return schema


def generate() -> str:
    """Return the JSON Schema as a formatted string."""
    schema = WorkflowDefinition.model_json_schema()
    schema = _strip_dead_fields(schema)
    return json.dumps(schema, indent=2) + "\n"


def main() -> None:
    """Generate or check the workflow definition schema."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed schema matches models (for CI).",
    )
    args = parser.parse_args()

    current = generate()

    if args.check:
        if not SCHEMA_PATH.exists():
            print(f"FAIL: {SCHEMA_PATH} does not exist. Run: uv run python schema/generate.py")
            sys.exit(1)
        committed = SCHEMA_PATH.read_text()
        if committed != current:
            print(f"FAIL: {SCHEMA_PATH} is stale. Run: uv run python schema/generate.py")
            sys.exit(1)
        print(f"OK: {SCHEMA_PATH} matches models.")
        return

    SCHEMA_PATH.write_text(current)
    print(f"Wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
