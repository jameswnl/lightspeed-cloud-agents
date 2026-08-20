"""Tests for the LocalExecutor implementing WorkflowExecutor interface."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


def _make_input(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal workflow run input."""
    return {
        "definition": {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test-workflow"},
            "spec": {"steps": steps},
        },
        "provider": {
            "name": "openai",
            "model": "gpt-4o",
            "credentials_secret": "openai-api-key",
        },
        "sandbox_image": "sandbox:latest",
    }


@pytest.fixture(name="mock_spawner")
def mock_spawner_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock spawner."""
    spawner = mocker.AsyncMock()
    spawner.spawn.return_value = "http://pod-1:8080"
    spawner.wait_ready.return_value = True
    return spawner


@pytest.fixture(name="mock_store")
def mock_store_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock RunStateStore."""
    store = mocker.AsyncMock()
    store.create = mocker.AsyncMock()
    store.get = mocker.AsyncMock(return_value=None)
    store.update_step = mocker.AsyncMock()
    store.append_event = mocker.AsyncMock()
    store.set_paused = mocker.AsyncMock()
    store.resume = mocker.AsyncMock()
    store.mark_terminal = mocker.AsyncMock()
    store.list_paused = mocker.AsyncMock(return_value=[])
    return store


@pytest.fixture(name="executor")
def executor_fixture(mock_spawner: AsyncMock, mock_store: AsyncMock) -> Any:
    """Create a LocalExecutor with mocked dependencies."""
    from cloud_agents.workflow.executor.local.executor import LocalExecutor

    return LocalExecutor(spawner=mock_spawner, run_state_store=mock_store)


class TestLocalExecutorStart:
    """Tests for starting workflows."""

    @pytest.mark.asyncio
    async def test_start_returns_workflow_id(
        self, executor: Any, mocker: MockerFixture
    ) -> None:
        """start() returns a workflow ID."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "done"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_input([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
        ])

        wf_id = await executor.start(input_data)
        assert wf_id.startswith("wf-")

    @pytest.mark.asyncio
    async def test_start_duplicate_raises(
        self, executor: Any, mock_store: AsyncMock, mocker: MockerFixture
    ) -> None:
        """start() with duplicate workflow_id raises ValueError."""
        import asyncpg

        mock_store.create.side_effect = ValueError("already exists")

        input_data = _make_input([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
        ])
        input_data["workflow_id"] = "wf-duplicate"

        with pytest.raises(ValueError, match="already exists"):
            await executor.start(input_data)


class TestLocalExecutorStatus:
    """Tests for querying workflow status."""

    @pytest.mark.asyncio
    async def test_get_status_missing_raises(self, executor: Any) -> None:
        """get_status() raises KeyError for nonexistent workflow."""
        with pytest.raises(KeyError):
            await executor.get_status("wf-nonexistent")

    @pytest.mark.asyncio
    async def test_get_status_returns_state(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """get_status() returns WorkflowStatus from store."""
        mock_store.get.return_value = {
            "workflow_id": "wf-1",
            "workflow_name": "test",
            "status": "completed",
            "current_step": None,
            "steps": {"r1": {"status": "completed", "output": {"ok": True}}},
            "events": [{"type": "step.completed"}],
            "definition": {},
            "provider": {},
            "authz_context": {},
            "workflow_context": {},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }

        status = await executor.get_status("wf-1")
        assert status.workflow_id == "wf-1"
        assert status.status == "completed"
        assert status.is_terminal is True


class TestLocalExecutorApproval:
    """Tests for approval signal handling."""

    @pytest.mark.asyncio
    async def test_approve_resumes_paused(
        self, executor: Any, mock_store: AsyncMock, mocker: MockerFixture
    ) -> None:
        """approve() resumes a paused workflow."""
        from cloud_agents.workflow.executor.base import ApprovalDecision

        mock_store.get.return_value = {
            "workflow_id": "wf-1",
            "workflow_name": "test",
            "status": "paused",
            "current_step": "approve-fix",
            "steps": {"approval": {"status": "awaiting_approval"}},
            "events": [],
            "definition": {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "test"},
                "spec": {"steps": [
                    {"name": "approve-fix", "type": "human-approval", "output_key": "approval", "message": "Approve?"},
                    {"name": "fix", "type": "agent", "prompt": "Fix", "output_key": "fix_result"},
                ]},
            },
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "authz_context": {},
            "workflow_context": {},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }

        await executor.approve(
            "wf-1",
            ApprovalDecision(step_name="approve-fix", decision="approved"),
        )

        mock_store.resume.assert_called_once_with("wf-1")

    @pytest.mark.asyncio
    async def test_approve_not_paused_raises(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """approve() raises RuntimeError if workflow is not paused."""
        from cloud_agents.workflow.executor.base import ApprovalDecision

        mock_store.get.return_value = {
            "workflow_id": "wf-1",
            "status": "running",
            "current_step": None,
        }

        with pytest.raises(RuntimeError, match="not paused"):
            await executor.approve(
                "wf-1",
                ApprovalDecision(step_name="approve-fix", decision="approved"),
            )

    @pytest.mark.asyncio
    async def test_approve_wrong_step_raises(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """approve() raises RuntimeError if paused at different step."""
        from cloud_agents.workflow.executor.base import ApprovalDecision

        mock_store.get.return_value = {
            "workflow_id": "wf-1",
            "status": "paused",
            "current_step": "approve-other",
        }

        with pytest.raises(RuntimeError, match="approve-other"):
            await executor.approve(
                "wf-1",
                ApprovalDecision(step_name="approve-fix", decision="approved"),
            )


class TestLocalExecutorCancel:
    """Tests for workflow cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_marks_terminal(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """cancel() marks workflow as cancelled."""
        mock_store.get.return_value = {
            "workflow_id": "wf-1",
            "status": "running",
        }

        await executor.cancel("wf-1")
        mock_store.mark_terminal.assert_called_once_with("wf-1", "cancelled")

    @pytest.mark.asyncio
    async def test_cancel_missing_raises(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """cancel() raises KeyError for nonexistent workflow."""
        mock_store.get.return_value = None

        with pytest.raises(KeyError):
            await executor.cancel("wf-missing")


class TestLocalExecutorIsTerminal:
    """Tests for terminal state check."""

    @pytest.mark.asyncio
    async def test_completed_is_terminal(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """Completed workflow is terminal."""
        mock_store.get.return_value = {"status": "completed"}
        assert await executor.is_terminal("wf-1") is True

    @pytest.mark.asyncio
    async def test_running_is_not_terminal(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """Running workflow is not terminal."""
        mock_store.get.return_value = {"status": "running"}
        assert await executor.is_terminal("wf-1") is False


class TestLocalExecutorNoTemporal:
    """Verify no Temporal imports."""

    def test_no_temporal_imports(self) -> None:
        """local_executor module has zero temporalio imports."""
        from cloud_agents.workflow.executor.local import executor as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source
