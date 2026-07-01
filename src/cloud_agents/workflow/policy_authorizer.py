"""YAML policy file based authorization.

Loads authorization rules from a YAML policy file and evaluates them
against caller identity, requested action, and target resource.
Supports identity patterns (user, team, service account, anonymous),
workflow name glob patterns, and owner-scoped conditions.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

import yaml
from pydantic import BaseModel

from cloud_agents.workflow.authorization import (
    AuthzDecision,
    CallerIdentity,
    WorkflowAction,
    WorkflowAuthorizer,
    WorkflowResource,
)

logger = logging.getLogger(__name__)


class PolicyDefaults(BaseModel):
    """Default authorization behavior when no rule matches.

    Attributes:
        allow: Actions allowed by default for all users.
        deny_unless_matched: Actions denied unless an explicit rule matches.
    """

    allow: list[str] = ["view"]
    deny_unless_matched: list[str] = [
        "trigger",
        "approve",
        "cancel",
        "manage_defs",
    ]


class PolicyRule(BaseModel):
    """A single authorization rule in the policy file.

    Attributes:
        identity: Identity pattern to match, e.g. "user:admin", "team:sre",
            "sa:default:bot", or "anonymous".
        actions: List of action strings this rule permits, e.g. ["trigger", "approve"].
        workflows: Workflow name glob patterns this rule applies to.
        conditions: Optional conditions for finer-grained control,
            e.g. {"require_owner": True}.
    """

    identity: str
    actions: list[str]
    workflows: list[str] = ["*"]
    conditions: dict[str, Any] | None = None


class PolicyConfig(BaseModel):
    """Top-level policy configuration loaded from YAML.

    Attributes:
        rules: Ordered list of authorization rules, evaluated first-match-wins.
        defaults: Default allow/deny behavior when no rule matches.
    """

    rules: list[PolicyRule] = []
    defaults: PolicyDefaults = PolicyDefaults()


class PolicyFileAuthorizer(WorkflowAuthorizer):
    """Authorizer that evaluates rules from a YAML policy file.

    Rules are evaluated in order; the first matching rule wins.
    If no rule matches, default allow/deny lists determine the outcome.
    """

    def __init__(self, policy_path: str) -> None:
        """Load and validate the policy file.

        Parameters:
            policy_path: Path to the YAML policy file.
        """
        with open(policy_path) as f:
            data = yaml.safe_load(f) or {}
        self.config = PolicyConfig.model_validate(data)
        logger.info(
            "Loaded policy file %s with %d rules",
            policy_path,
            len(self.config.rules),
        )

    async def authorize(
        self,
        identity: CallerIdentity,
        action: WorkflowAction,
        resource: WorkflowResource,
    ) -> AuthzDecision:
        """Evaluate policy rules against the request.

        Parameters:
            identity: The authenticated caller identity.
            action: The workflow action being attempted.
            resource: The target resource for the action.

        Returns:
            AuthzDecision with allowed=True/False and reason.
        """
        action_str = action.value

        # Check explicit rules in order (first match wins)
        for rule in self.config.rules:
            if not self._identity_matches(rule.identity, identity):
                continue
            if action_str not in rule.actions:
                continue
            if not self._workflow_matches(rule.workflows, resource):
                continue
            # Check conditions
            if rule.conditions and not self._conditions_met(rule.conditions, identity, resource):
                return AuthzDecision(
                    allowed=False,
                    reason=f"rule {rule.identity} matched but conditions not met "
                    f"(owner mismatch: resource owner={resource.owner!r})",
                )
            return AuthzDecision(
                allowed=True,
                reason=f"rule: {rule.identity}",
            )

        # Fall back to defaults
        if action_str in self.config.defaults.allow:
            return AuthzDecision(allowed=True, reason="default allow")

        return AuthzDecision(allowed=False, reason="no matching rule")

    def _identity_matches(self, pattern: str, identity: CallerIdentity) -> bool:
        """Check whether an identity pattern matches the caller.

        Supported patterns:
        - "anonymous": matches username "anonymous"
        - "user:<name>": matches exact username
        - "team:<name>": matches if <name> or "team:<name>" is in caller groups
        - "sa:<namespace>:<name>": matches "system:serviceaccount:<namespace>:<name>"
        - bare string: matches exact username

        Parameters:
            pattern: The identity pattern from the policy rule.
            identity: The caller identity to match against.

        Returns:
            True if the pattern matches the identity.
        """
        if pattern == "anonymous":
            return identity.username == "anonymous"

        if pattern.startswith("user:"):
            return identity.username == pattern.removeprefix("user:")

        if pattern.startswith("team:"):
            team = pattern.removeprefix("team:")
            return team in identity.groups or f"team:{team}" in identity.groups

        if pattern.startswith("sa:"):
            sa_suffix = pattern.removeprefix("sa:")
            return identity.username == f"system:serviceaccount:{sa_suffix}"

        return pattern == identity.username

    def _workflow_matches(self, patterns: list[str], resource: WorkflowResource) -> bool:
        """Check whether workflow name glob patterns match the resource.

        Parameters:
            patterns: List of glob patterns for workflow names.
            resource: The target workflow resource.

        Returns:
            True if any pattern matches, or if no workflow context is set.
        """
        if not resource.workflow_name:
            return True  # no workflow context = match any
        return any(fnmatch.fnmatch(resource.workflow_name, p) for p in patterns)

    def _conditions_met(
        self,
        conditions: dict[str, Any],
        identity: CallerIdentity,
        resource: WorkflowResource,
    ) -> bool:
        """Evaluate rule conditions against the request context.

        Currently supported conditions:
        - require_owner (bool): If true, caller username must match resource owner.

        Parameters:
            conditions: Condition dictionary from the policy rule.
            identity: The caller identity.
            resource: The target resource.

        Returns:
            True if all conditions are satisfied.
        """
        if conditions.get("require_owner"):
            if resource.owner and identity.username != resource.owner:
                return False
        return True
