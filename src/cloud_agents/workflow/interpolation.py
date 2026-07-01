"""Prompt template interpolation for workflow steps.

Resolves {{ steps.X.output.path }} placeholders from workflow state.
Supports nested paths: dot-separated keys and [N] array indices.
Values are wrapped in <data>...</data> delimiters to help LLMs
distinguish injected data from instructions.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final

from cloud_agents.workflow.state import WorkflowState

logger = logging.getLogger(__name__)

MAX_INTERPOLATED_VALUE_LENGTH: Final[int] = 10000

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


def _sanitize_value(value_str: str, step_ref: str) -> str:
    """Sanitize a resolved value string before template insertion.

    Prevents recursive template injection by detecting template syntax
    in interpolated values, and truncates oversized values.

    Args:
        value_str: The JSON-serialized value string.
        step_ref: Human-readable reference (e.g. "steps.s1.output.x") for logging.

    Returns:
        The sanitized value string, possibly truncated.
    """
    if "{{" in value_str:
        logger.warning(
            "Interpolated value for '%s' contains template syntax; "
            "it will NOT be recursively expanded",
            step_ref,
        )
    if len(value_str) > MAX_INTERPOLATED_VALUE_LENGTH:
        logger.warning(
            "Interpolated value for '%s' truncated from %d to %d characters",
            step_ref,
            len(value_str),
            MAX_INTERPOLATED_VALUE_LENGTH,
        )
        value_str = value_str[:MAX_INTERPOLATED_VALUE_LENGTH] + "..."
    return value_str


def _format_value(value: Any, step_ref: str = "") -> str:
    """Format a resolved value for template insertion.

    All values are JSON-serialized to prevent delimiter injection
    in the <data>...</data> boundary.  Values are then sanitized
    to prevent recursive template expansion and limit size.

    Args:
        value: The resolved value to format.
        step_ref: Human-readable reference for logging.

    Returns:
        Formatted and sanitized string wrapped in <data> delimiters.
    """
    if value is None:
        return "<data>null</data>"
    serialized = json.dumps(value)
    sanitized = _sanitize_value(serialized, step_ref)
    return f"<data>{sanitized}</data>"


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
        step_ref = f"steps.{step_name}.output.{path}"
        result = state.steps.get(step_name)
        if result is None or result.output is None:
            raise ValueError(
                f"Template references missing step or output: {step_ref}"
            )
        if "." not in path and "[" not in path:
            value = result.output.get(path)
        else:
            value = resolve_path(result.output, path)
        return _format_value(value, step_ref)

    return TEMPLATE_PATTERN.sub(replacer, template)
