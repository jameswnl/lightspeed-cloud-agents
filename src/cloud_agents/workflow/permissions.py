"""Per-task permission scoping for workflow steps.

Defines permission scope model and validation for constraining
what tools and resources a workflow step can access.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PermissionScope(BaseModel):
    """Permission constraints for a workflow step.

    Attributes:
        service_account: K8s ServiceAccount for spawned pods.
        allowed_tools: Whitelist of tool names the agent may use.
        denied_tools: Blacklist of tool names to exclude.
        max_tokens: Maximum token budget for this step.
        timeout_seconds: Maximum duration for this step.
    """

    service_account: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    denied_tools: Optional[list[str]] = None
    max_tokens: Optional[int] = Field(default=None, gt=0)
    timeout_seconds: Optional[int] = Field(default=None, gt=0)

    def effective_tools(self, all_tools: list[str]) -> list[str]:
        """Compute the effective tool set after filtering.

        Args:
            all_tools: Full list of available tool names.

        Returns:
            Filtered list of tool names.
        """
        result = list(all_tools)
        if self.allowed_tools is not None:
            allowed = set(self.allowed_tools)
            result = [t for t in result if t in allowed]
        if self.denied_tools is not None:
            denied = set(self.denied_tools)
            result = [t for t in result if t not in denied]
        return result
