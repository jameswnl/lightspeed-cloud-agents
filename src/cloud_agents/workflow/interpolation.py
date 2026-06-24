"""Prompt template interpolation for workflow steps.

Resolves {{ steps.X.output.path }} placeholders from workflow state.
Supports nested paths: dot-separated keys and [N] array indices.
Values are wrapped in <data>...</data> delimiters to help LLMs
distinguish injected data from instructions.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agents.workflow.state import WorkflowState

TEMPLATE_PATTERN = re.compile(
    r"\{\{\s*steps\.(\w+)\.output\.([\w]+(?:\[\d+\])?(?:\.[\w]+(?:\[\d+\])?)*)\s*\}\}"
)

_SEGMENT_PATTERN = re.compile(r"(\w+)(?:\[(\d+)\])?")


def resolve_path(data: Any, path: str) -> Any:
    """Walk a dot-separated path with optional [N] array indices.

    Args:
        data: Root data structure (dict/list).
        path: Dot-separated path, e.g. "details.host" or "actions[0].host".

    Returns:
        The resolved value.

    Raises:
        ValueError: If any segment is missing, out of range, or type-mismatched.
    """
    current = data
    segments = path.split(".")
    visited: list[str] = []

    for segment in segments:
        m = _SEGMENT_PATTERN.fullmatch(segment)
        if not m:
            raise ValueError(f"Invalid path segment: '{segment}'")

        key, index_str = m.group(1), m.group(2)

        if not isinstance(current, dict):
            raise ValueError(
                f"Path '{'.'.join(visited)}' is not a dict, "
                f"cannot access key '{key}'"
            )
        if key not in current:
            loc = ".".join(visited) or "root"
            raise ValueError(f"Key '{key}' not found at '{loc}'")
        current = current[key]
        visited.append(key)

        if index_str is not None:
            idx = int(index_str)
            if not isinstance(current, list):
                raise ValueError(
                    f"Path '{'.'.join(visited)}' is not a list, "
                    f"cannot index with [{idx}]"
                )
            if idx >= len(current):
                raise ValueError(
                    f"Index [{idx}] out of range at '{'.'.join(visited)}' "
                    f"(length {len(current)})"
                )
            current = current[idx]
            visited[-1] = f"{key}[{idx}]"

    return current


def _format_value(value: Any) -> str:
    """Format a resolved value for template insertion.

    All values are JSON-serialized to prevent delimiter injection
    in the <data>...</data> boundary.
    """
    if value is None:
        return "<data>null</data>"
    return f"<data>{json.dumps(value)}</data>"


def interpolate(template: str, state: WorkflowState) -> str:
    """Replace {{ steps.X.output.path }} with values from workflow state.

    Supports nested paths including dot-separated keys and [N] array
    indices, e.g. {{ steps.X.output.actions[0].host }}.

    Args:
        template: Prompt template with {{ }} placeholders.
        state: Current workflow state with step results.

    Returns:
        Interpolated prompt string.

    Raises:
        ValueError: If a referenced step, output key, or path is missing.
    """

    def replacer(match: re.Match) -> str:
        step_name, path = match.group(1), match.group(2)
        result = state.steps.get(step_name)
        if result is None or result.output is None:
            raise ValueError(
                f"Template references missing step or output: "
                f"steps.{step_name}.output.{path}"
            )
        if "." not in path and "[" not in path:
            value = result.output.get(path)
        else:
            value = resolve_path(result.output, path)
        return _format_value(value)

    return TEMPLATE_PATTERN.sub(replacer, template)
