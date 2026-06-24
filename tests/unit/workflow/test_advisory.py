"""Unit tests for advisory mode enforcement."""

from __future__ import annotations

import pytest

from agents.workflow.advisory import ADVISORY_PROMPT_SUFFIX, AdvisoryEnforcer


class TestAdvisoryEnforcerDisabled:
    """Tests when advisory mode is disabled."""

    def test_prompt_unchanged(self) -> None:
        """Test that prompts pass through unchanged."""
        enforcer = AdvisoryEnforcer(enabled=False)
        assert enforcer.annotate_prompt("Check hosts") == "Check hosts"

    def test_tools_unchanged(self) -> None:
        """Test that all tools pass through."""
        enforcer = AdvisoryEnforcer(enabled=False)
        tools = [("list_hosts", lambda: None), ("run_fix", lambda: None)]
        assert enforcer.filter_tools(tools, ["list_hosts"]) == tools

    def test_output_unchanged(self) -> None:
        """Test that output is not annotated."""
        enforcer = AdvisoryEnforcer(enabled=False)
        output = {"summary": "ok"}
        assert enforcer.annotate_output(output) == {"summary": "ok"}

    def test_approval_not_skipped(self) -> None:
        """Test that approval steps are not skipped."""
        enforcer = AdvisoryEnforcer(enabled=False)
        assert not enforcer.should_skip_approval()


class TestAdvisoryEnforcerEnabled:
    """Tests when advisory mode is enabled."""

    def test_prompt_annotated(self) -> None:
        """Test that advisory suffix is appended to prompt."""
        enforcer = AdvisoryEnforcer(enabled=True)
        result = enforcer.annotate_prompt("Check hosts")
        assert result == "Check hosts" + ADVISORY_PROMPT_SUFFIX

    def test_tools_filtered_to_read_only(self) -> None:
        """Test that only read-only tools are kept."""
        enforcer = AdvisoryEnforcer(enabled=True)
        fn_a = lambda: "a"
        fn_b = lambda: "b"
        fn_c = lambda: "c"
        tools = [("list_hosts", fn_a), ("check_host", fn_b), ("run_fix", fn_c)]
        filtered = enforcer.filter_tools(tools, ["list_hosts", "check_host"])
        assert len(filtered) == 2
        assert filtered[0][0] == "list_hosts"
        assert filtered[1][0] == "check_host"

    def test_tools_no_classification_warns(self) -> None:
        """Test that missing classification returns all tools with warning."""
        enforcer = AdvisoryEnforcer(enabled=True)
        tools = [("list_hosts", lambda: None), ("run_fix", lambda: None)]
        result = enforcer.filter_tools(tools, None)
        assert len(result) == 2

    def test_tools_empty_classification_warns(self) -> None:
        """Test that empty classification returns all tools."""
        enforcer = AdvisoryEnforcer(enabled=True)
        tools = [("list_hosts", lambda: None)]
        result = enforcer.filter_tools(tools, [])
        assert len(result) == 1

    def test_output_annotated(self) -> None:
        """Test that output gets advisory marker."""
        enforcer = AdvisoryEnforcer(enabled=True)
        output = {"summary": "disk full"}
        result = enforcer.annotate_output(output)
        assert result["advisory"] is True
        assert result["summary"] == "disk full"

    def test_output_does_not_mutate_original(self) -> None:
        """Test that annotate_output returns a new dict."""
        enforcer = AdvisoryEnforcer(enabled=True)
        output = {"summary": "ok"}
        result = enforcer.annotate_output(output)
        assert "advisory" not in output
        assert "advisory" in result

    def test_approval_skipped(self) -> None:
        """Test that approval steps are skipped."""
        enforcer = AdvisoryEnforcer(enabled=True)
        assert enforcer.should_skip_approval()
