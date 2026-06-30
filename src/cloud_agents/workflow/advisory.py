"""Advisory mode enforcement for workflow execution.

Two-layer enforcement:
1. Tool filtering: removes write-capable tools when read_only classification exists
2. Prompt annotation: appends advisory instructions to agent prompts
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

ADVISORY_PROMPT_SUFFIX = (
    "\n\nADVISORY MODE: Diagnose only. "
    "Report what you would do without taking action."
)


class AdvisoryEnforcer:
    """Enforces advisory mode constraints on workflow steps.

    Attributes:
        _enabled: Whether advisory mode is active.
    """

    def __init__(self, enabled: bool = False) -> None:
        """Initialize the enforcer.

        Args:
            enabled: Whether advisory mode is active for this workflow.
        """
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Whether advisory mode is active."""
        return self._enabled

    def annotate_prompt(self, prompt: str) -> str:
        """Append advisory instructions to a prompt.

        Args:
            prompt: The original agent prompt.

        Returns:
            Annotated prompt if advisory mode is enabled, otherwise unchanged.
        """
        if not self._enabled:
            return prompt
        return prompt + ADVISORY_PROMPT_SUFFIX

    def filter_tools(
        self,
        tools: list[tuple[str, Any]],
        read_only_tools: Optional[list[str]] = None,
    ) -> list[tuple[str, Any]]:
        """Filter tools to read-only set in advisory mode.

        Args:
            tools: List of (name, callable) tool pairs.
            read_only_tools: Names of tools classified as read-only.

        Returns:
            Filtered tool list. If advisory mode is off, returns all tools.
            If no read_only classification exists, returns all tools with a warning.
        """
        if not self._enabled:
            return tools

        if not read_only_tools:
            logger.warning(
                "Advisory mode enabled but no read_only tool classification provided. "
                "All tools remain available — tool filtering is not enforced."
            )
            return tools

        read_only_set = set(read_only_tools)
        filtered = [(name, fn) for name, fn in tools if name in read_only_set]
        removed = [name for name, _ in tools if name not in read_only_set]
        if removed:
            logger.info(
                "Advisory mode: removed %d write-capable tools: %s",
                len(removed),
                removed,
            )
        return filtered

    def should_skip_approval(self) -> bool:
        """Whether approval steps should be skipped in advisory mode."""
        return self._enabled

    def annotate_output(self, output: dict[str, Any]) -> dict[str, Any]:
        """Add advisory marker to step output.

        Args:
            output: The step's output dict.

        Returns:
            Output dict with advisory: true added if enabled.
        """
        if not self._enabled:
            return output
        return {**output, "advisory": True}
