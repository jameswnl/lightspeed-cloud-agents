"""Escalation packaging for workflow failures.

Packages escalation handoff documents and sends them to external
systems (Jira, webhook, CLI handoff, or structured log).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EscalationPackage(BaseModel):
    """Complete escalation package for external systems.

    Attributes:
        workflow_name: Name of the failed workflow.
        step_name: Step that exhausted retries.
        correlation_id: Trace correlation ID.
        timestamp: When the escalation was generated.
        escalation: The raw escalation handoff data.
        workflow_snapshot: Current state of all workflow steps.
        definition: Original workflow definition dict.
        input_prompt: User-provided input prompt for the workflow.
        events: Workflow event timeline.
        provider_name: LLM provider name used by the workflow.
        workflow_id: Unique workflow execution identifier.
    """

    workflow_name: str
    step_name: str
    correlation_id: Optional[str] = None
    timestamp: str
    escalation: dict[str, Any]
    workflow_snapshot: dict[str, Any]
    definition: Optional[dict[str, Any]] = None
    input_prompt: Optional[str] = None
    events: Optional[list[dict[str, Any]]] = None
    provider_name: Optional[str] = None
    workflow_id: Optional[str] = None
    step_transcripts: Optional[dict[str, Any]] = None


class EscalationPackager(Protocol):
    """Protocol for escalation delivery implementations."""

    async def package(self, pkg: EscalationPackage) -> None:
        """Deliver an escalation package."""
        ...


class LogPackager:
    """Logs escalation as structured JSON (default for PoC)."""

    async def package(self, pkg: EscalationPackage) -> None:
        """Log the escalation package."""
        logger.error(
            "ESCALATION: %s",
            json.dumps(pkg.model_dump(mode="json"), indent=2),
        )


class WebhookPackager:
    """Sends escalation to a generic webhook.

    Attributes:
        url: Webhook endpoint URL.
    """

    def __init__(self, url: str) -> None:
        """Initialize the webhook packager.

        Args:
            url: Webhook endpoint URL.
        """
        self.url = url

    async def package(self, pkg: EscalationPackage) -> None:
        """POST the escalation package to the webhook."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.url,
                    json=pkg.model_dump(mode="json"),
                )
                resp.raise_for_status()
            logger.info("Escalation sent to webhook for step '%s'", pkg.step_name)
        except Exception as exc:
            logger.warning(
                "Escalation webhook failed for step '%s': %s", pkg.step_name, exc
            )


class JiraPackager:
    """Creates a Jira issue with the escalation details.

    Attributes:
        url: Jira REST API base URL.
        project_key: Jira project key.
    """

    def __init__(self, url: str, project_key: str) -> None:
        """Initialize the Jira packager.

        Args:
            url: Jira REST API base URL.
            project_key: Jira project key for issue creation.
        """
        self.url = url
        self.project_key = project_key

    async def package(self, pkg: EscalationPackage) -> None:
        """Create a Jira issue with the escalation details."""
        issue_data = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": f"Escalation: {pkg.workflow_name} / {pkg.step_name}",
                "description": json.dumps(pkg.model_dump(mode="json"), indent=2),
                "issuetype": {"name": "Bug"},
            }
        }
        auth_token = __import__("os").environ.get("JIRA_API_TOKEN", "")
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.url}/rest/api/2/issue",
                    json=issue_data,
                    headers=headers,
                )
                resp.raise_for_status()
            logger.info(
                "Jira issue created for escalation: %s/%s",
                pkg.workflow_name,
                pkg.step_name,
            )
        except Exception as exc:
            logger.warning(
                "Jira escalation failed for step '%s': %s", pkg.step_name, exc
            )


def build_escalation_package(
    workflow_name: str,
    step_name: str,
    escalation_data: dict[str, Any],
    workflow_snapshot: dict[str, Any],
    correlation_id: Optional[str] = None,
    definition: Optional[dict[str, Any]] = None,
    input_prompt: Optional[str] = None,
    events: Optional[list[dict[str, Any]]] = None,
    provider_name: Optional[str] = None,
    workflow_id: Optional[str] = None,
    step_transcripts: Optional[dict[str, Any]] = None,
) -> EscalationPackage:
    """Build an escalation package from workflow state.

    Args:
        workflow_name: Name of the failed workflow.
        step_name: Step that exhausted retries.
        escalation_data: Raw escalation handoff dict.
        workflow_snapshot: Current workflow state snapshot.
        correlation_id: Optional trace correlation ID.
        definition: Original workflow definition dict.
        input_prompt: User-provided input prompt.
        events: Workflow event timeline.
        provider_name: LLM provider name.
        workflow_id: Unique workflow execution ID.
        step_transcripts: Optional step transcript data keyed by output_key.

    Returns:
        Complete EscalationPackage ready for delivery.
    """
    return EscalationPackage(
        workflow_name=workflow_name,
        step_name=step_name,
        correlation_id=correlation_id,
        timestamp=datetime.now(UTC).isoformat(),
        escalation=escalation_data,
        workflow_snapshot=workflow_snapshot,
        definition=definition,
        input_prompt=input_prompt,
        events=events,
        provider_name=provider_name,
        workflow_id=workflow_id,
        step_transcripts=step_transcripts,
    )


def serialize_handoff_context(pkg: EscalationPackage) -> str:
    """Serialize an escalation package into a markdown context document.

    Produces a structured markdown document suitable for loading into
    Claude Code or similar interactive CLI tools.

    Args:
        pkg: The escalation package to serialize.

    Returns:
        Markdown string with workflow context for human handoff.
    """
    import yaml

    sections: list[str] = []

    sections.append(f"# Investigation Handoff: {pkg.workflow_name}")
    sections.append("")

    # What happened
    sections.append("## What happened")
    if pkg.input_prompt:
        sections.append(pkg.input_prompt)
    else:
        sections.append("No input prompt provided.")
    sections.append("")

    # Workflow definition
    if pkg.definition:
        sections.append("## Workflow definition")
        sections.append("```yaml")
        sections.append(yaml.dump(pkg.definition, default_flow_style=False).rstrip())
        sections.append("```")
        sections.append("")

    # Step results
    sections.append("## Step results")
    for step_key, step_data in pkg.workflow_snapshot.items():
        if isinstance(step_data, dict):
            step_status = step_data.get("status", "unknown")
            sections.append(f"### {step_key} -- {step_status}")
            if output := step_data.get("output"):
                sections.append(f"**Output**: {json.dumps(output, indent=2)}")
            if error := step_data.get("error"):
                sections.append(f"**Error**: {error}")
            sections.append("")

    # Event timeline
    if pkg.events:
        sections.append("## Event timeline")
        for event in pkg.events:
            ts = event.get("timestamp", "")
            etype = event.get("type", "")
            step = event.get("step", "")
            sections.append(f"- {ts} -- {etype} / {step}")
        sections.append("")

    # What failed
    sections.append("## What failed")
    sections.append(
        f"Step '{pkg.step_name}' failed."
    )
    if pkg.escalation.get("failure_history"):
        last_error = pkg.escalation["failure_history"][-1].get("error", "unknown")
        sections.append(f"Last error: {last_error}")

    # Render agent tool call chain from transcript (if available)
    if pkg.step_transcripts:
        for step_key, step_data in pkg.workflow_snapshot.items():
            if isinstance(step_data, dict) and step_data.get("status") == "failed":
                transcript = pkg.step_transcripts.get(step_key)
                if transcript and transcript.get("events"):
                    sections.append("")
                    sections.append(f"### Agent reasoning for {step_key}")
                    for event in transcript["events"]:
                        event_type = event.get("type", "unknown")
                        data = event.get("data", {})
                        ts = event.get("ts", "")
                        if event_type in ("tool_call", "tool_result"):
                            name = data.get("name", "unknown")
                            duration = data.get("duration_ms")
                            input_val = data.get("input", "")
                            duration_str = f" ({duration}ms)" if duration else ""
                            input_summary = (
                                str(input_val)[:100] if input_val else ""
                            )
                            sections.append(
                                f"- [{ts}] **{name}**{duration_str}: {input_summary}"
                            )
                        elif event_type == "error":
                            msg = data.get("message", "unknown error")
                            sections.append(f"- [{ts}] ERROR: {msg}")
                        elif event_type == "thinking":
                            text = data.get("text", "")
                            sections.append(f"- [{ts}] Thinking: {text[:200]}")
    sections.append("")

    # Provider info
    if pkg.provider_name:
        sections.append("## Provider")
        sections.append(f"Provider: {pkg.provider_name}")
        sections.append("")

    # Suggested next steps
    sections.append("## Suggested next steps")
    sections.append(f"1. Investigate the root cause of the '{pkg.step_name}' failure")
    if pkg.provider_name:
        sections.append(
            f"2. Check the provider ({pkg.provider_name}) for availability issues"
        )
    sections.append("")

    # Launch command
    wf_id = pkg.workflow_id or pkg.workflow_name
    sections.append("## CLI launch command")
    sections.append("```bash")
    sections.append(
        f'claude -p "Continue this investigation. '
        f"The workflow '{pkg.workflow_name}' (ID: {wf_id}) needs attention. "
        f'Read the context above for details."'
    )
    sections.append("```")

    return "\n".join(sections)


class CLIHandoffPackager:
    """Generate a markdown context file and CLI launch command.

    Writes a structured markdown document for human consumption
    with pre-loaded context from a failed workflow.

    Attributes:
        output_dir: Directory to write context files to.
    """

    def __init__(self, output_dir: str = "/tmp/cloud-agents-handoff") -> None:
        """Initialize the CLI handoff packager.

        Args:
            output_dir: Directory for writing context files.
        """
        self.output_dir = output_dir

    async def package(self, pkg: EscalationPackage) -> None:
        """Write handoff context to file and log the launch command."""
        try:
            context_md = serialize_handoff_context(pkg)
            out_dir = Path(self.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            wf_id = pkg.workflow_id or pkg.workflow_name
            filename = f"handoff-{wf_id}.md"
            context_file = out_dir / filename
            context_file.write_text(context_md)

            launch_cmd = (
                f'claude -p "Continue this investigation. '
                f"Read the context file at {context_file} first.\""
            )
            logger.info(
                "CLI handoff ready:\n  Context: %s\n  Launch:  %s",
                context_file,
                launch_cmd,
            )
        except Exception as exc:
            logger.warning(
                "CLI handoff packaging failed for step '%s': %s",
                pkg.step_name,
                exc,
            )
