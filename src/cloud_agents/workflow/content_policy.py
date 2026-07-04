"""Configurable content policy for workflow definition validation.

Validates definition content at submission time, enforcing organizational
policies such as prompt length limits, blocked instruction patterns,
required fields, and namespace restrictions.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import yaml
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)


class BlockedPattern(BaseModel):
    """A regex pattern that rejects prompts containing dangerous instructions.

    Attributes:
        pattern: Regex pattern to match against prompt and instructions text.
        reason: Human-readable reason why this pattern is blocked.
    """

    pattern: str
    reason: str

    @field_validator("pattern")
    @classmethod
    def pattern_must_be_valid_regex(cls, v: str) -> str:
        """Validate that the pattern is a compilable regex."""
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc
        return v


class ContentPolicy(BaseModel):
    """Configurable content policy for workflow definitions.

    Attributes:
        max_prompt_length: Maximum allowed prompt length in characters.
        blocked_patterns: Regex patterns that reject matching prompts.
        required_fields: Step fields that must be present (e.g. output_schema).
        allowed_namespaces: If set, restricts target_namespaces to this allowlist.
    """

    max_prompt_length: int = 10_000
    blocked_patterns: list[BlockedPattern] = []
    required_fields: list[str] = []
    allowed_namespaces: Optional[list[str]] = None


class ContentPolicyViolation(BaseModel):
    """A single content policy violation.

    Attributes:
        rule: The policy rule that was violated.
        step_name: The step where the violation occurred.
        reason: Human-readable description of the violation.
    """

    rule: str
    step_name: str
    reason: str


def evaluate_content_policy(
    defn: dict[str, Any],
    policy: ContentPolicy | None,
) -> list[ContentPolicyViolation]:
    """Evaluate a workflow definition against a content policy.

    Parameters:
        defn: The workflow definition dict to validate.
        policy: The content policy to enforce. If None, returns empty list.

    Returns:
        List of policy violations. Empty list means compliant.
    """
    if policy is None:
        return []

    violations: list[ContentPolicyViolation] = []
    spec = defn.get("spec", {})
    steps = spec.get("steps", [])

    for step in steps:
        step_name = step.get("name", "unknown")
        prompt = step.get("prompt") or ""
        instructions = step.get("instructions") or ""

        # --- Max prompt/instructions length ---
        if prompt and len(prompt) > policy.max_prompt_length:
            violations.append(
                ContentPolicyViolation(
                    rule="max_prompt_length",
                    step_name=step_name,
                    reason=(
                        f"Prompt exceeds {policy.max_prompt_length} chars "
                        f"(got {len(prompt)})"
                    ),
                )
            )
        if instructions and len(instructions) > policy.max_prompt_length:
            violations.append(
                ContentPolicyViolation(
                    rule="max_prompt_length",
                    step_name=step_name,
                    reason=(
                        f"Instructions exceeds {policy.max_prompt_length} chars "
                        f"(got {len(instructions)})"
                    ),
                )
            )

        # --- Blocked patterns ---
        for bp in policy.blocked_patterns:
            compiled = re.compile(bp.pattern, re.IGNORECASE)
            matched_in: list[str] = []
            if prompt and compiled.search(prompt):
                matched_in.append("prompt")
            if instructions and compiled.search(instructions):
                matched_in.append("instructions")
            if matched_in:
                fields_str = ", ".join(matched_in)
                violations.append(
                    ContentPolicyViolation(
                        rule="blocked_pattern",
                        step_name=step_name,
                        reason=f"{bp.reason} (matched in {fields_str})",
                    )
                )

        # --- Required fields ---
        for field in policy.required_fields:
            if not step.get(field):
                violations.append(
                    ContentPolicyViolation(
                        rule="required_field",
                        step_name=step_name,
                        reason=f"Step '{step_name}' is missing required field '{field}'",
                    )
                )

        # --- Namespace restrictions ---
        if policy.allowed_namespaces is not None:
            target_ns = step.get("target_namespaces") or []
            for ns in target_ns:
                if ns not in policy.allowed_namespaces:
                    violations.append(
                        ContentPolicyViolation(
                            rule="allowed_namespaces",
                            step_name=step_name,
                            reason=(
                                f"Namespace '{ns}' is not in the allowed list: "
                                f"{policy.allowed_namespaces}"
                            ),
                        )
                    )

    return violations


def load_content_policy(path: str) -> ContentPolicy:
    """Load a content policy from a YAML file.

    The YAML file should have a top-level ``content_policy`` key with
    the policy configuration. If the key is missing or the file is empty,
    a default ContentPolicy is returned.

    Parameters:
        path: Path to the YAML policy file.

    Returns:
        ContentPolicy loaded from the file.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    policy_data = data.get("content_policy", {})
    policy = ContentPolicy.model_validate(policy_data)
    logger.info("Loaded content policy from %s", path)
    return policy
