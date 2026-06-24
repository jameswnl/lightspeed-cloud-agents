"""Unit tests for per-task permission scoping."""

from __future__ import annotations

import pytest

from agents.workflow.permissions import PermissionScope


class TestPermissionScope:
    """Tests for PermissionScope model."""

    def test_defaults(self) -> None:
        """Test default values."""
        scope = PermissionScope()
        assert scope.service_account is None
        assert scope.allowed_tools is None
        assert scope.denied_tools is None

    def test_allowed_tools_filter(self) -> None:
        """Test that allowed_tools whitelist filters correctly."""
        scope = PermissionScope(allowed_tools=["list_hosts", "check_host"])
        result = scope.effective_tools(["list_hosts", "check_host", "run_fix"])
        assert result == ["list_hosts", "check_host"]

    def test_denied_tools_filter(self) -> None:
        """Test that denied_tools blacklist filters correctly."""
        scope = PermissionScope(denied_tools=["run_fix"])
        result = scope.effective_tools(["list_hosts", "check_host", "run_fix"])
        assert result == ["list_hosts", "check_host"]

    def test_both_filters(self) -> None:
        """Test that both allowed and denied work together."""
        scope = PermissionScope(
            allowed_tools=["list_hosts", "check_host", "run_fix"],
            denied_tools=["run_fix"],
        )
        result = scope.effective_tools(["list_hosts", "check_host", "run_fix", "other"])
        assert result == ["list_hosts", "check_host"]

    def test_no_filters_returns_all(self) -> None:
        """Test that no filters returns all tools."""
        scope = PermissionScope()
        result = scope.effective_tools(["list_hosts", "check_host"])
        assert result == ["list_hosts", "check_host"]

    def test_max_tokens_validation(self) -> None:
        """Test that max_tokens must be positive."""
        with pytest.raises(Exception):
            PermissionScope(max_tokens=0)

    def test_service_account(self) -> None:
        """Test ServiceAccount field."""
        scope = PermissionScope(service_account="restricted-sa")
        assert scope.service_account == "restricted-sa"
