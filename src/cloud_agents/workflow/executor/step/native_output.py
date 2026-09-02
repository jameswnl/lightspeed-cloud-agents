"""Shared schema guard for native structured-output mode.

`direct.py` (spawn: none) and `subprocess_child.py` (spawn: local) each
attempt pydantic-ai's output_mode="native" for object-rooted output_schema,
falling back to a prompt-text schema hint otherwise. This is the single
source of truth for that eligibility check so the two executors can't drift
on which schema shapes are attempted natively -- see #235 and its follow-up.

No temporalio imports.
"""

from __future__ import annotations

from typing import Any


def supports_native_output(output_schema: dict[str, Any]) -> bool:
    """Whether output_schema's root shape is safe to send via native structured output.

    output_schema is user-authored workflow YAML, not internally
    guaranteed to be an object-rooted JSON Schema. OpenAI's Structured
    Outputs (what output_mode="native" maps to) requires an object root
    -- a top-level array or anyOf/oneOf/allOf union can be rejected by
    the provider. That rejection isn't always a pydantic-ai UserError, so
    it isn't guaranteed to be caught by a UserError-based fallback -- the
    schema shape has to be checked before attempting native mode, not
    recovered from after.

    Parameters:
        output_schema: The step's requested JSON Schema.

    Returns:
        True if output_schema has an object root.
    """
    return output_schema.get("type") == "object"
