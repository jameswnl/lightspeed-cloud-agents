"""Safe condition evaluator for workflow step conditions.

Parses a restricted grammar — no eval(), no arbitrary code.
Supports: steps.X.status, steps.X.approved, steps.X.output.Y
Operators: ==, !=, truthy check
Combinators: and, or
"""

from __future__ import annotations

import re
from typing import Any

from agents.workflow.state import WorkflowState

CONDITION_PATTERN = re.compile(
    r"steps\.(\w+)\.(status|approved|output\.(\w+))\s*(==|!=)?\s*(.*)$"
)


def evaluate_condition(condition: str, state: WorkflowState) -> bool:
    """Evaluate a condition expression against workflow state.

    Args:
        condition: Expression like "steps.X.output.Y == true".
        state: Current workflow state.

    Returns:
        True if the condition is satisfied.

    Raises:
        ValueError: If the condition cannot be parsed.
    """
    condition = condition.strip()

    if " and " in condition:
        parts = condition.split(" and ")
        return all(evaluate_condition(p.strip(), state) for p in parts)
    if " or " in condition:
        parts = condition.split(" or ")
        return any(evaluate_condition(p.strip(), state) for p in parts)

    match = CONDITION_PATTERN.match(condition)
    if not match:
        raise ValueError(f"Unparseable condition: {condition!r}")

    step_name = match.group(1)
    field_path = match.group(2)
    output_key = match.group(3)
    operator = match.group(4)
    raw_value = (match.group(5) or "").strip()

    result = state.steps.get(step_name)
    if result is None:
        return False

    actual: Any
    if field_path == "status":
        actual = result.status
    elif field_path == "approved":
        actual = result.output.get("approved", False) if result.output else False
    elif output_key:
        actual = result.output.get(output_key) if result.output else None
    else:
        return False

    if not operator:
        return bool(actual)

    expected: Any
    if raw_value == "true":
        expected = True
    elif raw_value == "false":
        expected = False
    elif raw_value == "null":
        expected = None
    elif raw_value.startswith('"') and raw_value.endswith('"'):
        expected = raw_value[1:-1]
    elif raw_value.startswith("'") and raw_value.endswith("'"):
        expected = raw_value[1:-1]
    else:
        expected = raw_value

    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected

    return False
