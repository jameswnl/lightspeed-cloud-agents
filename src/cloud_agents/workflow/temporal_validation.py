"""Workflow definition validation.

Catches errors at submission time rather than deep in workflow execution.
"""

from __future__ import annotations

import re
from typing import Any


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

    return errors
