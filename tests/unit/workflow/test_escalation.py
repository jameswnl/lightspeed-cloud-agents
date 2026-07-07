"""Unit tests for escalation packaging."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloud_agents.workflow.escalation import (
    CLIHandoffPackager,
    EscalationPackage,
    JiraPackager,
    LogPackager,
    WebhookPackager,
    build_escalation_package,
    serialize_handoff_context,
)


def _make_package() -> EscalationPackage:
    """Create a test escalation package."""
    return build_escalation_package(
        workflow_name="diagnose-fix",
        step_name="fix-hosts",
        escalation_data={
            "failure_history": [{"error": "timeout"}],
            "recommendation": "manual fix",
        },
        workflow_snapshot={"diagnosis": {"summary": "3 hosts down"}},
        correlation_id="corr-123",
    )


def _make_enriched_package() -> EscalationPackage:
    """Create a test escalation package with enriched fields."""
    return build_escalation_package(
        workflow_name="diagnose-fix",
        step_name="fix-hosts",
        escalation_data={
            "failure_history": [{"error": "timeout"}],
            "recommendation": "manual fix",
        },
        workflow_snapshot={
            "r1": {
                "status": "completed",
                "output": {"summary": "found 3 issues"},
            },
            "r2": {
                "status": "failed",
                "error": "retries exhausted",
            },
        },
        correlation_id="corr-123",
        definition={"metadata": {"name": "diagnose-fix"}, "spec": {"steps": []}},
        input_prompt="Fix the broken hosts in cluster-1",
        events=[
            {"type": "step.started", "step": "diagnose", "timestamp": "2026-01-01T00:00:00"},
            {"type": "step.completed", "step": "diagnose", "timestamp": "2026-01-01T00:01:00"},
            {"type": "step.started", "step": "fix-hosts", "timestamp": "2026-01-01T00:02:00"},
            {"type": "step.failed", "step": "fix-hosts", "timestamp": "2026-01-01T00:03:00"},
        ],
        provider_name="openai",
        workflow_id="wf-abc123",
    )


class TestBuildEscalationPackage:
    """Tests for build_escalation_package."""

    def test_creates_package(self) -> None:
        """Test that a package is created with all fields."""
        pkg = _make_package()
        assert pkg.workflow_name == "diagnose-fix"
        assert pkg.step_name == "fix-hosts"
        assert pkg.correlation_id == "corr-123"
        assert pkg.timestamp is not None
        assert pkg.escalation["recommendation"] == "manual fix"
        assert pkg.workflow_snapshot["diagnosis"]["summary"] == "3 hosts down"


class TestLogPackager:
    """Tests for LogPackager."""

    @pytest.mark.asyncio
    async def test_logs_without_error(self) -> None:
        """Test that LogPackager logs the escalation."""
        packager = LogPackager()
        await packager.package(_make_package())


class TestWebhookPackager:
    """Tests for WebhookPackager."""

    @pytest.mark.asyncio
    async def test_sends_payload(self) -> None:
        """Test that webhook sends correct payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.escalation.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            packager = WebhookPackager("http://hooks.example.com/escalation")
            await packager.package(_make_package())

            payload = mock_client.post.call_args[1]["json"]
            assert payload["workflow_name"] == "diagnose-fix"
            assert payload["step_name"] == "fix-hosts"

    @pytest.mark.asyncio
    async def test_failure_does_not_raise(self) -> None:
        """Test that webhook failures are logged, not raised."""
        with patch("cloud_agents.workflow.escalation.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("down"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            packager = WebhookPackager("http://hooks.example.com/escalation")
            await packager.package(_make_package())


class TestJiraPackager:
    """Tests for JiraPackager."""

    @pytest.mark.asyncio
    async def test_creates_issue(self) -> None:
        """Test that Jira packager sends correct issue payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.escalation.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            packager = JiraPackager("https://jira.example.com", "OPS")
            await packager.package(_make_package())

            call_args = mock_client.post.call_args
            assert "/rest/api/2/issue" in call_args[0][0]
            payload = call_args[1]["json"]
            assert payload["fields"]["project"]["key"] == "OPS"
            assert "fix-hosts" in payload["fields"]["summary"]


class TestEnrichedEscalationPackage:
    """Tests for enriched EscalationPackage fields (T15 Task 1)."""

    def test_new_fields_populated(self) -> None:
        """Enriched package has definition, input_prompt, events, provider_name, workflow_id."""
        pkg = _make_enriched_package()
        assert pkg.definition is not None
        assert pkg.definition["metadata"]["name"] == "diagnose-fix"
        assert pkg.input_prompt == "Fix the broken hosts in cluster-1"
        assert pkg.events is not None
        assert len(pkg.events) == 4
        assert pkg.provider_name == "openai"
        assert pkg.workflow_id == "wf-abc123"

    def test_new_fields_optional_defaults_none(self) -> None:
        """New fields default to None when not provided."""
        pkg = _make_package()
        assert pkg.definition is None
        assert pkg.input_prompt is None
        assert pkg.events is None
        assert pkg.provider_name is None
        assert pkg.workflow_id is None

    def test_backward_compatible_serialization(self) -> None:
        """Existing code that creates packages without new fields still works."""
        pkg = _make_package()
        data = pkg.model_dump(mode="json")
        assert "definition" in data
        assert data["definition"] is None
        assert "workflow_name" in data
        assert data["workflow_name"] == "diagnose-fix"


class TestSerializeHandoffContext:
    """Tests for serialize_handoff_context (T15 Task 2)."""

    def test_contains_workflow_name(self) -> None:
        """Context markdown contains the workflow name as heading."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "# Investigation Handoff: diagnose-fix" in md

    def test_contains_input_prompt(self) -> None:
        """Context markdown contains what happened section with input prompt."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "Fix the broken hosts in cluster-1" in md

    def test_contains_step_results(self) -> None:
        """Context markdown contains step results with status and output."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "r1" in md
        assert "completed" in md
        assert "found 3 issues" in md
        assert "r2" in md
        assert "failed" in md

    def test_contains_event_timeline(self) -> None:
        """Context markdown contains event timeline."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "step.started" in md
        assert "step.completed" in md
        assert "step.failed" in md

    def test_contains_failure_info(self) -> None:
        """Context markdown contains what failed section."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "fix-hosts" in md
        assert "retries exhausted" in md.lower() or "failed" in md.lower()

    def test_contains_provider_info(self) -> None:
        """Context markdown contains provider information."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "openai" in md

    def test_contains_definition_yaml(self) -> None:
        """Context markdown contains the workflow definition."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "diagnose-fix" in md
        assert "definition" in md.lower() or "workflow" in md.lower()

    def test_contains_launch_command(self) -> None:
        """Context markdown contains a CLI launch command."""
        pkg = _make_enriched_package()
        md = serialize_handoff_context(pkg)
        assert "claude" in md

    def test_minimal_package_still_works(self) -> None:
        """Serialization works with a package that has no enriched fields."""
        pkg = _make_package()
        md = serialize_handoff_context(pkg)
        assert "# Investigation Handoff: diagnose-fix" in md
        assert "fix-hosts" in md

    def test_no_input_prompt_shows_placeholder(self) -> None:
        """When input_prompt is None, a placeholder is shown."""
        pkg = _make_package()
        md = serialize_handoff_context(pkg)
        assert "no input prompt" in md.lower() or "not provided" in md.lower()


class TestCLIHandoffPackager:
    """Tests for CLIHandoffPackager (T15 Task 3)."""

    @pytest.mark.asyncio
    async def test_writes_context_file(self, tmp_path: str) -> None:
        """CLIHandoffPackager writes a markdown context file."""
        packager = CLIHandoffPackager(output_dir=str(tmp_path))
        pkg = _make_enriched_package()
        await packager.package(pkg)

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".md"
        content = files[0].read_text()
        assert "Investigation Handoff" in content

    @pytest.mark.asyncio
    async def test_context_file_contains_launch_command(self, tmp_path: str) -> None:
        """Context file includes a claude launch command."""
        packager = CLIHandoffPackager(output_dir=str(tmp_path))
        pkg = _make_enriched_package()
        await packager.package(pkg)

        files = list(tmp_path.iterdir())
        content = files[0].read_text()
        assert "claude" in content

    @pytest.mark.asyncio
    async def test_creates_output_directory(self, tmp_path: str) -> None:
        """CLIHandoffPackager creates the output directory if it doesn't exist."""
        output_dir = str(tmp_path / "subdir" / "handoff")
        packager = CLIHandoffPackager(output_dir=output_dir)
        pkg = _make_enriched_package()
        await packager.package(pkg)

        assert os.path.isdir(output_dir)

    @pytest.mark.asyncio
    async def test_logs_launch_command(self, tmp_path: str, caplog: pytest.LogCaptureFixture) -> None:
        """CLIHandoffPackager logs the context file path and launch command."""
        import logging

        with caplog.at_level(logging.INFO, logger="cloud_agents.workflow.escalation"):
            packager = CLIHandoffPackager(output_dir=str(tmp_path))
            pkg = _make_enriched_package()
            await packager.package(pkg)

        assert any("CLI handoff ready" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_failure_does_not_raise(self, tmp_path: str) -> None:
        """CLIHandoffPackager does not raise on write failure."""
        # Use a path that can't be created (file in place of directory)
        blocker = tmp_path / "blocker"
        blocker.write_text("blocking")
        bad_dir = str(blocker / "impossible")

        packager = CLIHandoffPackager(output_dir=bad_dir)
        pkg = _make_enriched_package()
        # Should not raise
        await packager.package(pkg)
