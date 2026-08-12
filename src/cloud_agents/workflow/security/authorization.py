"""Workflow authorization models and framework.

Defines identity, action, resource, and decision models for workflow
authorization, plus the authorizer interface and noop implementation.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from enum import Enum

from fastapi import HTTPException, Request
from pydantic import BaseModel


class CallerIdentity(BaseModel):
    """Authenticated caller identity attached to request state.

    Attributes:
        username: Caller username, e.g. "system:serviceaccount:default:sre-bot".
        uid: K8s UID from TokenReview, if available.
        groups: Groups or teams the caller belongs to.
        auth_mode: Authentication method used: "shared_secret", "sa_token", or "jwt".
    """

    username: str
    uid: str | None = None
    groups: list[str] = []
    auth_mode: str  # "shared_secret", "sa_token", "jwt"


class WorkflowAction(str, Enum):
    """Actions that can be authorized on workflow resources."""

    TRIGGER = "trigger"
    APPROVE = "approve"
    VIEW = "view"
    CANCEL = "cancel"
    VIEW_DEFS = "view_defs"
    MANAGE_DEFS = "manage_defs"
    SCHEDULE_CREATE = "schedule_create"
    SCHEDULE_VIEW = "schedule_view"
    SCHEDULE_DELETE = "schedule_delete"
    SCHEDULE_PAUSE = "schedule_pause"
    SCHEDULE_RESUME = "schedule_resume"
    SESSION_MESSAGE = "session_message"


class WorkflowResource(BaseModel):
    """Resource target for an authorization check.

    Attributes:
        workflow_id: Unique workflow run identifier.
        workflow_name: Workflow definition name.
        owner: Username of the workflow owner.
        namespace: K8s namespace scope, if applicable.
        step: Specific workflow step, if applicable.
    """

    workflow_id: str | None = None
    workflow_name: str | None = None
    owner: str | None = None
    namespace: str | None = None
    step: str | None = None


class AuthzDecision(BaseModel):
    """Result of an authorization check.

    Attributes:
        allowed: Whether the action is permitted.
        reason: Human-readable explanation of the decision.
    """

    allowed: bool
    reason: str = ""


class ApproverInfo(BaseModel):
    """Identity of a workflow step approver.

    Attributes:
        username: Approver's username.
        uid: Approver's UID, if available.
        approved_at: ISO 8601 timestamp of when approval was given.
    """

    username: str
    uid: str | None = None
    approved_at: str


class WorkflowAuthzContext(BaseModel):
    """Immutable authorization context captured at workflow trigger time.

    Attributes:
        owner_username: Who triggered the workflow.
        owner_groups: Caller's groups/teams at trigger time.
        workflow_name: Definition name for pattern matching.
        namespace: K8s namespace from caller SA or config.
    """

    owner_username: str
    owner_groups: list[str] = []
    workflow_name: str
    namespace: str | None = None


class WorkflowAuthorizer(ABC):
    """Decides whether a caller can perform an action on a workflow."""

    @abstractmethod
    async def authorize(
        self,
        identity: CallerIdentity,
        action: WorkflowAction,
        resource: WorkflowResource,
    ) -> AuthzDecision:
        """Check if identity can perform action on resource.

        Parameters:
            identity: The authenticated caller identity.
            action: The workflow action being attempted.
            resource: The target resource for the action.

        Returns:
            AuthzDecision with allowed=True/False and reason.
        """
        ...


class NoopAuthorizer(WorkflowAuthorizer):
    """Authorizer that permits all actions.

    Used when authorization is disabled (WORKFLOW_AUTHZ=none) or for
    backward-compatible deployments with shared secret auth.
    """

    async def authorize(
        self,
        identity: CallerIdentity,
        action: WorkflowAction,
        resource: WorkflowResource,
    ) -> AuthzDecision:
        """Allow all actions unconditionally.

        Parameters:
            identity: The authenticated caller identity.
            action: The workflow action being attempted.
            resource: The target resource for the action.

        Returns:
            AuthzDecision with allowed=True.
        """
        return AuthzDecision(allowed=True, reason="authorization disabled")


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency to extract caller identity from request state.

    Fails closed when authorization is enabled but no identity is present.
    The "anonymous" identity is only produced by explicit shared-secret auth,
    never as a fallback for missing request state.

    Parameters:
        request: The incoming FastAPI request.

    Returns:
        CallerIdentity extracted from request state or anonymous fallback.

    Raises:
        HTTPException: 401 when authorization is enabled but no identity found.
    """
    identity = getattr(request.state, "caller_identity", None)
    if identity is not None:
        return identity

    # No identity attached — check if this is expected
    authz_mode = os.environ.get("WORKFLOW_AUTHZ", "none")
    if authz_mode != "none":
        # Authorization enabled but no authenticated identity — fail closed
        raise HTTPException(
            status_code=401,
            detail="Authorization is enabled but no caller identity was extracted. "
            "Ensure authentication is configured correctly.",
        )

    # Authorization disabled — anonymous is acceptable
    return CallerIdentity(username="anonymous", auth_mode="shared_secret")


def parse_namespace_from_sa_username(username: str) -> str | None:
    """Extract namespace from K8s ServiceAccount username format.

    ServiceAccount usernames follow the pattern:
    ``system:serviceaccount:{namespace}:{name}``

    Parameters:
        username: The username string to parse.

    Returns:
        The namespace if the username matches the SA format, None otherwise.
    """
    parts = username.split(":")
    if len(parts) == 4 and parts[0] == "system" and parts[1] == "serviceaccount":
        return parts[2]
    return None
