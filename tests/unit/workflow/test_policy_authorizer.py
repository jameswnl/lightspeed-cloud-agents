"""Unit tests for YAML policy file based authorization."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cloud_agents.workflow.authorization import (
    CallerIdentity,
    WorkflowAction,
    WorkflowResource,
)
from cloud_agents.workflow.policy_authorizer import PolicyFileAuthorizer


def _write_policy(tmp_path: Path, data: dict) -> str:
    """Write a policy YAML file and return its path."""
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.dump(data))
    return str(policy_path)


def _make_identity(
    username: str = "testuser",
    uid: str | None = "uid-123",
    groups: list[str] | None = None,
    auth_mode: str = "token",
) -> CallerIdentity:
    """Create a CallerIdentity for testing."""
    return CallerIdentity(
        username=username,
        uid=uid,
        groups=groups or [],
        auth_mode=auth_mode,
    )


def _make_resource(
    workflow_name: str | None = "diagnose-prod",
    workflow_id: str = "wf-1",
    owner: str = "admin",
    namespace: str = "default",
    step: str | None = None,
) -> WorkflowResource:
    """Create a WorkflowResource for testing."""
    return WorkflowResource(
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        owner=owner,
        namespace=namespace,
        step=step,
    )


class TestPolicyFileAuthorizer:
    """Tests for PolicyFileAuthorizer."""

    @pytest.mark.asyncio
    async def test_explicit_allow_rule(self, tmp_path: Path) -> None:
        """Rule allowing user:admin to trigger grants access."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:admin",
                        "actions": ["trigger", "approve"],
                        "workflows": ["*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="admin"),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is True
        assert "user:admin" in decision.reason

    @pytest.mark.asyncio
    async def test_explicit_deny_no_match(self, tmp_path: Path) -> None:
        """User not matching any rule is denied for trigger."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:admin",
                        "actions": ["trigger"],
                        "workflows": ["*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="nobody"),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is False
        assert "no matching rule" in decision.reason

    @pytest.mark.asyncio
    async def test_wildcard_workflow_match(self, tmp_path: Path) -> None:
        """Rule with workflows: ['*'] matches any workflow name."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:operator",
                        "actions": ["trigger"],
                        "workflows": ["*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="operator"),
            WorkflowAction.TRIGGER,
            _make_resource(workflow_name="anything-at-all"),
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_workflow_pattern_match(self, tmp_path: Path) -> None:
        """Rule with workflows: ['diagnose-*'] matches 'diagnose-prod' but not 'fix-prod'."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:sre-eng",
                        "actions": ["trigger"],
                        "workflows": ["diagnose-*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)

        # Should match diagnose-prod
        decision = await authorizer.authorize(
            _make_identity(username="sre-eng"),
            WorkflowAction.TRIGGER,
            _make_resource(workflow_name="diagnose-prod"),
        )
        assert decision.allowed is True

        # Should NOT match fix-prod
        decision = await authorizer.authorize(
            _make_identity(username="sre-eng"),
            WorkflowAction.TRIGGER,
            _make_resource(workflow_name="fix-prod"),
        )
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_default_allow_view(self, tmp_path: Path) -> None:
        """No explicit rule for user, but view is in defaults.allow."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [],
                "defaults": {
                    "allow": ["view", "view_defs"],
                    "deny_unless_matched": ["trigger", "approve", "cancel", "manage_defs"],
                },
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="random-user"),
            WorkflowAction.VIEW,
            _make_resource(),
        )
        assert decision.allowed is True
        assert "default allow" in decision.reason

    @pytest.mark.asyncio
    async def test_default_deny_trigger(self, tmp_path: Path) -> None:
        """No explicit rule, trigger is in deny_unless_matched."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [],
                "defaults": {
                    "allow": ["view"],
                    "deny_unless_matched": ["trigger", "approve", "cancel", "manage_defs"],
                },
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="random-user"),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_identity_matching_sa_token(self, tmp_path: Path) -> None:
        """Rule identity 'sa:default:sre-bot' matches 'system:serviceaccount:default:sre-bot'."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "sa:default:sre-bot",
                        "actions": ["trigger", "approve"],
                        "workflows": ["*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="system:serviceaccount:default:sre-bot"),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_identity_matching_team_group(self, tmp_path: Path) -> None:
        """Rule identity 'team:sre' matches caller with 'sre' in groups."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "team:sre",
                        "actions": ["trigger", "approve"],
                        "workflows": ["*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)

        # Match with bare group name
        decision = await authorizer.authorize(
            _make_identity(username="alice", groups=["sre", "developers"]),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is True

        # Match with prefixed group name
        decision = await authorizer.authorize(
            _make_identity(username="bob", groups=["team:sre"]),
            WorkflowAction.APPROVE,
            _make_resource(),
        )
        assert decision.allowed is True

        # No match — different team
        decision = await authorizer.authorize(
            _make_identity(username="charlie", groups=["qa", "team:qa"]),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_identity_matching_anonymous(self, tmp_path: Path) -> None:
        """Rule identity 'anonymous' matches caller with username 'anonymous'."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "anonymous",
                        "actions": ["view"],
                        "workflows": ["*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="anonymous"),
            WorkflowAction.VIEW,
            _make_resource(),
        )
        assert decision.allowed is True

        # Non-anonymous user should not match anonymous rule
        decision = await authorizer.authorize(
            _make_identity(username="someone"),
            WorkflowAction.VIEW,
            _make_resource(),
        )
        # Should still be allowed via defaults (view is default allow)
        # but not via the anonymous rule specifically
        assert decision.allowed is True  # default allow kicks in

    @pytest.mark.asyncio
    async def test_no_workflow_context_matches_any(self, tmp_path: Path) -> None:
        """When resource has no workflow_name, any workflow pattern matches."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:admin",
                        "actions": ["view_defs"],
                        "workflows": ["diagnose-*"],
                    }
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="admin"),
            WorkflowAction.VIEW_DEFS,
            _make_resource(workflow_name=None),
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_multiple_rules_first_match_wins(self, tmp_path: Path) -> None:
        """When multiple rules match, the first one wins."""
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:admin",
                        "actions": ["trigger"],
                        "workflows": ["diagnose-*"],
                    },
                    {
                        "identity": "user:admin",
                        "actions": ["trigger"],
                        "workflows": ["*"],
                    },
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)
        decision = await authorizer.authorize(
            _make_identity(username="admin"),
            WorkflowAction.TRIGGER,
            _make_resource(workflow_name="diagnose-prod"),
        )
        assert decision.allowed is True
        # First rule should match
        assert "user:admin" in decision.reason

    @pytest.mark.asyncio
    async def test_empty_policy_uses_defaults(self, tmp_path: Path) -> None:
        """Empty policy with no rules uses default allow/deny."""
        policy_path = _write_policy(tmp_path, {})
        authorizer = PolicyFileAuthorizer(policy_path)

        # View should be allowed by default
        decision = await authorizer.authorize(
            _make_identity(username="anyone"),
            WorkflowAction.VIEW,
            _make_resource(),
        )
        assert decision.allowed is True

        # Trigger should be denied by default
        decision = await authorizer.authorize(
            _make_identity(username="anyone"),
            WorkflowAction.TRIGGER,
            _make_resource(),
        )
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_owner_scoped_rule(self, tmp_path: Path) -> None:
        """Rule with conditions.require_owner allows only workflow owners.

        TODO: Full owner-scoped authorization is a future enhancement.
        This test validates the basic condition structure is accepted
        and that owner matching works when conditions include require_owner.
        """
        policy_path = _write_policy(
            tmp_path,
            {
                "rules": [
                    {
                        "identity": "user:alice",
                        "actions": ["approve"],
                        "workflows": ["*"],
                        "conditions": {"require_owner": True},
                    },
                ],
            },
        )
        authorizer = PolicyFileAuthorizer(policy_path)

        # Alice is the owner — should be allowed
        decision = await authorizer.authorize(
            _make_identity(username="alice"),
            WorkflowAction.APPROVE,
            _make_resource(owner="alice"),
        )
        assert decision.allowed is True

        # Alice is NOT the owner — should be denied
        decision = await authorizer.authorize(
            _make_identity(username="alice"),
            WorkflowAction.APPROVE,
            _make_resource(owner="bob"),
        )
        assert decision.allowed is False
        assert "owner" in decision.reason.lower()
