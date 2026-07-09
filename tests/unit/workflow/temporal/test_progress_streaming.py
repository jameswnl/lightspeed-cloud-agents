"""Unit tests for progress streaming in temporal_activities (TDD).

Tests the OpenShell-specific progress streaming wired into
run_sandbox_step, including heartbeat truncation, graceful
degradation for non-OpenShell spawners, and SSE event forwarding.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from cloud_agents.workflow.temporal_activities import (
    _truncate_heartbeat_payload,
    compute_pod_name,
    run_sandbox_step,
)

# Pre-compute the pod_name for default test inputs so we can set
# the correct sandbox_id key on the spawner.
_DEFAULT_POD_NAME = compute_pod_name("wf-1", "diag", 1)


def _make_step_input(step_name: str = "diag") -> dict[str, Any]:
    """Build a standard sandbox step input dict."""
    return {
        "step": {"name": step_name, "prompt": "diagnose", "output_key": "r1"},
        "workflow_id": "wf-1",
        "provider": {
            "name": "openai",
            "model": "gpt-4",
            "credentials_secret": "k",
        },
        "sandbox_image": "sandbox:latest",
        "context": {},
    }


def _mock_http_success(mocker: MockerFixture) -> MagicMock:
    """Set up httpx.AsyncClient mock returning success=True."""
    mock_response = mocker.MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "success": True,
        "output": {"summary": "diagnosed ok"},
    }

    mock_http = mocker.patch(
        "cloud_agents.workflow.temporal_activities.httpx.AsyncClient",
    )
    mock_client_instance = mocker.MagicMock(
        post=mocker.AsyncMock(return_value=mock_response),
    )
    mock_http.return_value.__aenter__ = mocker.AsyncMock(
        return_value=mock_client_instance,
    )
    mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)
    return mock_http


class TestProgressStreamingWithOpenShell:
    """Tests for progress streaming when spawner is OpenShellSpawner."""

    @pytest.mark.asyncio
    async def test_progress_task_started_for_openshell_spawner(
        self, mocker: MockerFixture
    ) -> None:
        """Progress streaming task is started when spawner is OpenShellSpawner."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.AsyncMock()
        mock_client.create_sandbox.return_value = "sb-1"
        mock_client.expose_service.return_value = "http://sb-1:8080"

        # Make exec_stream a noop async generator for start_server
        async def noop_exec(*args, **kwargs):
            return
            yield

        mock_client.exec_stream = noop_exec

        spawner = OpenShellSpawner(openshell_client=mock_client)
        # Mock the base class methods
        mocker.patch.object(spawner, "spawn", return_value="http://sb-1:8080")
        mocker.patch.object(spawner, "wait_ready", return_value=True)
        mocker.patch.object(spawner, "destroy", return_value=None)
        spawner._sandbox_names[_DEFAULT_POD_NAME] = "sb-1"

        # Mock stream_progress to yield one event then stop
        async def mock_stream_progress(sandbox_id):
            yield {"type": "tool_call", "name": "get_pods", "ts": "t1"}

        mocker.patch.object(spawner, "stream_progress", side_effect=mock_stream_progress)

        mock_heartbeat = mocker.patch(
            "cloud_agents.workflow.temporal_activities.activity.heartbeat"
        )
        _mock_http_success(mocker)

        result = await run_sandbox_step(_make_step_input(), spawner=spawner)

        assert result["status"] == "completed"
        # Heartbeat should have been called with progress data at least once
        # (once from the progress task, plus periodic heartbeat loop)
        heartbeat_calls = mock_heartbeat.call_args_list
        progress_calls = [
            c for c in heartbeat_calls
            if c.args and isinstance(c.args[0], dict)
            and c.args[0].get("event_type") == "tool_call"
        ]
        assert len(progress_calls) >= 1

    @pytest.mark.asyncio
    async def test_progress_heartbeat_is_truncated(
        self, mocker: MockerFixture
    ) -> None:
        """Progress heartbeats contain truncated summaries under 1KB."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.AsyncMock())
        mocker.patch.object(spawner, "spawn", return_value="http://sb-1:8080")
        mocker.patch.object(spawner, "wait_ready", return_value=True)
        mocker.patch.object(spawner, "destroy", return_value=None)
        spawner._sandbox_names[_DEFAULT_POD_NAME] = "sb-1"

        # Yield event with very long input
        long_input = "x" * 5000

        async def mock_stream_progress(sandbox_id):
            yield {"type": "tool_call", "name": "get_pods", "input": long_input, "ts": "t1"}

        mocker.patch.object(spawner, "stream_progress", side_effect=mock_stream_progress)

        mock_heartbeat = mocker.patch(
            "cloud_agents.workflow.temporal_activities.activity.heartbeat"
        )
        _mock_http_success(mocker)

        await run_sandbox_step(_make_step_input(), spawner=spawner)

        progress_calls = [
            c for c in mock_heartbeat.call_args_list
            if c.args and isinstance(c.args[0], dict)
            and c.args[0].get("event_type") == "tool_call"
        ]
        for call in progress_calls:
            payload = json.dumps(call.args[0])
            assert len(payload) < 1024, f"Heartbeat payload too large: {len(payload)} bytes"

    @pytest.mark.asyncio
    async def test_progress_task_cancelled_on_http_completion(
        self, mocker: MockerFixture
    ) -> None:
        """Progress streaming task is cancelled when HTTP result returns."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.AsyncMock())
        mocker.patch.object(spawner, "spawn", return_value="http://sb-1:8080")
        mocker.patch.object(spawner, "wait_ready", return_value=True)
        mocker.patch.object(spawner, "destroy", return_value=None)
        spawner._sandbox_names[_DEFAULT_POD_NAME] = "sb-1"

        progress_cancelled = False

        async def slow_stream_progress(sandbox_id):
            nonlocal progress_cancelled
            try:
                while True:
                    yield {"type": "thinking", "ts": "t1"}
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                progress_cancelled = True
                raise

        mocker.patch.object(spawner, "stream_progress", side_effect=slow_stream_progress)

        mocker.patch("cloud_agents.workflow.temporal_activities.activity.heartbeat")
        _mock_http_success(mocker)

        result = await run_sandbox_step(_make_step_input(), spawner=spawner)

        assert result["status"] == "completed"
        # Give the cancellation a moment to propagate
        await asyncio.sleep(0.05)
        assert progress_cancelled, "Progress task should be cancelled after HTTP completion"


class TestGracefulDegradation:
    """Tests for graceful degradation with non-OpenShell spawners."""

    @pytest.mark.asyncio
    async def test_non_openshell_spawner_skips_progress(
        self, mocker: MockerFixture
    ) -> None:
        """Non-OpenShell spawners skip progress streaming entirely."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_heartbeat = mocker.patch(
            "cloud_agents.workflow.temporal_activities.activity.heartbeat"
        )
        _mock_http_success(mocker)

        result = await run_sandbox_step(_make_step_input(), spawner=mock_spawner)

        assert result["status"] == "completed"
        # Only periodic heartbeats, no progress heartbeats
        progress_calls = [
            c for c in mock_heartbeat.call_args_list
            if c.args and isinstance(c.args[0], dict)
            and "event_type" in c.args[0]
        ]
        assert len(progress_calls) == 0

    @pytest.mark.asyncio
    async def test_kubernetes_spawner_no_progress(
        self, mocker: MockerFixture
    ) -> None:
        """KubernetesSpawner (not OpenShell) gets no progress streaming."""
        # Just verify isinstance check works by using a plain AsyncMock
        # (which is not an OpenShellSpawner)
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        _mock_http_success(mocker)
        mocker.patch("cloud_agents.workflow.temporal_activities.activity.heartbeat")

        result = await run_sandbox_step(_make_step_input(), spawner=mock_spawner)

        assert result["status"] == "completed"


class TestProgressStreamError:
    """Tests for error handling in progress streaming."""

    @pytest.mark.asyncio
    async def test_progress_stream_error_does_not_affect_result(
        self, mocker: MockerFixture
    ) -> None:
        """Errors in progress streaming do not affect the HTTP result."""
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        spawner = OpenShellSpawner(openshell_client=mocker.AsyncMock())
        mocker.patch.object(spawner, "spawn", return_value="http://sb-1:8080")
        mocker.patch.object(spawner, "wait_ready", return_value=True)
        mocker.patch.object(spawner, "destroy", return_value=None)
        spawner._sandbox_names[_DEFAULT_POD_NAME] = "sb-1"

        async def failing_stream_progress(sandbox_id):
            yield {"type": "tool_call", "name": "a", "ts": "t1"}
            raise ConnectionError("stream dropped")

        mocker.patch.object(spawner, "stream_progress", side_effect=failing_stream_progress)

        mocker.patch("cloud_agents.workflow.temporal_activities.activity.heartbeat")
        _mock_http_success(mocker)

        result = await run_sandbox_step(_make_step_input(), spawner=spawner)

        # HTTP result is source of truth -- unaffected by stream error
        assert result["status"] == "completed"
        assert result["output"]["summary"] == "diagnosed ok"


class TestTruncateHeartbeatPayload:
    """Tests for the _truncate_heartbeat_payload helper."""

    def test_small_event_passes_through(self) -> None:
        """Events under 1KB pass through with key fields only."""
        event = {"type": "tool_call", "name": "get_pods", "ts": "2024-01-01T00:00:00Z"}
        result = _truncate_heartbeat_payload(event)

        assert result["event_type"] == "tool_call"
        assert result["tool"] == "get_pods"
        assert len(json.dumps(result)) < 1024

    def test_large_event_truncated(self) -> None:
        """Events with large payloads are truncated to summary only."""
        event = {
            "type": "tool_result",
            "name": "get_pods",
            "output": "x" * 5000,
            "ts": "2024-01-01T00:00:00Z",
        }
        result = _truncate_heartbeat_payload(event)

        assert result["event_type"] == "tool_result"
        assert result["tool"] == "get_pods"
        # No full output in heartbeat
        assert "output" not in result or len(json.dumps(result)) < 1024

    def test_thinking_event_summarized(self) -> None:
        """Thinking events include type but not full content."""
        event = {"type": "thinking", "content": "Let me analyze..." * 100}
        result = _truncate_heartbeat_payload(event)

        assert result["event_type"] == "thinking"
        assert len(json.dumps(result)) < 1024

    def test_unknown_event_type_included(self) -> None:
        """Unknown event types still produce a summary."""
        event = {"type": "custom_event", "data": "something"}
        result = _truncate_heartbeat_payload(event)

        assert result["event_type"] == "custom_event"
        assert len(json.dumps(result)) < 1024

    def test_missing_type_defaults(self) -> None:
        """Events without type field get 'unknown' type."""
        event = {"name": "tool_a"}
        result = _truncate_heartbeat_payload(event)

        assert result["event_type"] == "unknown"

    def test_truncates_long_event_type(self) -> None:
        """Long event_type is truncated to stay under limit."""
        event = {"type": "x" * 2000, "name": "tool"}
        result = _truncate_heartbeat_payload(event)
        assert len(result["event_type"]) <= 200

    def test_truncates_long_tool_name(self) -> None:
        """Long tool name is truncated to stay under limit."""
        event = {"type": "tool_call", "name": "x" * 2000}
        result = _truncate_heartbeat_payload(event)
        assert len(result.get("tool", "")) <= 200

    def test_total_payload_under_limit_with_huge_inputs(self) -> None:
        """Total JSON payload stays under 1KB even with huge inputs."""
        event = {"type": "x" * 5000, "name": "y" * 5000}
        result = _truncate_heartbeat_payload(event)
        encoded = json.dumps(result)
        assert len(encoded) <= 1024

    def test_normal_event_passes_through_unchanged(self) -> None:
        """Normal-sized events pass through with correct field mapping."""
        event = {"type": "tool_call", "name": "get_pods"}
        result = _truncate_heartbeat_payload(event)
        assert result == {"event_type": "tool_call", "tool": "get_pods"}
