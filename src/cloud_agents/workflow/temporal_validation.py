"""Workflow definition validation.

Catches errors at submission time rather than deep in workflow execution.
"""

from __future__ import annotations

import re
from typing import Any


def _validate_schema(
    schema: dict[str, Any],
    step_name: str,
    path: str,
    errors: list[str],
) -> None:
    """Recursively validate a JSON Schema fragment.

    Checks that array types have an 'items' definition.
    """
    schema_type = schema.get("type")

    if schema_type == "array" and "items" not in schema:
        errors.append(
            f"output_schema for step '{step_name}': "
            f"'{path}' is type 'array' but missing required 'items' definition"
        )

    if "items" in schema and isinstance(schema["items"], dict):
        _validate_schema(schema["items"], step_name, f"{path}.items", errors)

    for prop_name, prop_schema in schema.get("properties", {}).items():
        if isinstance(prop_schema, dict):
            _validate_schema(prop_schema, step_name, prop_name, errors)


def validate_definition(defn: dict[str, Any]) -> list[str]:
    """Validate a workflow definition dict.

    Returns a list of error messages. Empty list means valid.
    """
    errors: list[str] = []
    spec = defn.get("spec", {})
    steps = spec.get("steps", [])

    if not steps:
        errors.append("Workflow must have at least one step")
        return errors

    output_keys: set[str] = set()
    step_names: set[str] = set()

    for i, step in enumerate(steps):
        name = step.get("name")
        if not name:
            errors.append(f"Step {i} is missing required field 'name'")
            continue

        if name in step_names:
            errors.append(f"Duplicate step name: '{name}'")
        step_names.add(name)

        output_key = step.get("output_key")
        if output_key:
            if output_key in output_keys:
                errors.append(f"Duplicate output_key: '{output_key}' in step '{name}'")
            output_keys.add(output_key)

        prompt = step.get("prompt") or ""
        refs = re.findall(r"\{\{\s*steps\.(\w+)\.", prompt)
        for ref in refs:
            if ref not in output_keys:
                errors.append(
                    f"Step '{name}' references undefined step '{ref}' in prompt template"
                )

        output_schema = step.get("output_schema")
        if output_schema and isinstance(output_schema, dict):
            _validate_schema(output_schema, name, "root", errors)

    return errors
