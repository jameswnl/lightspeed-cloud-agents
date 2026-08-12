"""Tests for TemporalExecutor wrapping Temporal Client behind WorkflowExecutor."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
from temporalio.client import WorkflowExecutionStatus


@pytest.fixture(name="mock_client")
def mock_client_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock Temporal Client."""
    client = mocker.AsyncMock()
    client.start_workflow = mocker.AsyncMock(return_value="wf-temporal-1")

    handle = mocker.AsyncMock()
    handle.signal = mocker.AsyncMock()
    handle.cancel = mocker.AsyncMock()
    handle.query = mocker.AsyncMock(return_value={})
    handle.describe = mocker.AsyncMock(
        return_value=MagicMock(status=WorkflowExecutionStatus.RUNNING)
    )
    client.get_workflow_handle = MagicMock(return_value=handle)

    return client


@pytest.fixture(name="executor")
def executor_fixture(mock_client: AsyncMock) -> Any:
    """Create a TemporalExecutor with mocked client."""
    from cloud_agents.workflow.executor.temporal.executor import TemporalExecutor

    return TemporalExecutor(client=mock_client)


class TestTemporalExecutorStart:
    """Tests for starting workflows via Temporal."""

    @pytest.mark.asyncio
    async def test_start_calls_temporal(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """start() calls temporal_client.start_workflow."""
        input_data = {
            "definition": {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "test"},
                "spec": {"steps": []},
            },
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        }

        wf_id = await executor.start(input_data)
        assert wf_id is not None
        mock_client.start_workflow.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_duplicate_raises_value_error(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """start() translates WorkflowAlreadyStartedError to ValueError."""
        from temporalio.service import RPCError

        mock_client.start_workflow.side_effect = RPCError(
            "workflow execution already started", MagicMock(), b""
        )

        with pytest.raises(ValueError, match="already exists"):
            await executor.start({"definition": {}, "provider": {}})


class TestTemporalExecutorApprove:
    """Tests for approval signals."""

    @pytest.mark.asyncio
    async def test_approve_uses_positional_args(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """approve() sends signal with args=[] not a dict."""
        from cloud_agents.workflow.executor.base import ApprovalDecision

        await executor.approve(
            "wf-1",
            ApprovalDecision(
                step_name="approve-fix",
                decision="approved",
                approver="user:james",
            ),
        )

        handle = mock_client.get_workflow_handle.return_value
        handle.signal.assert_called_once()
        call_kwargs = handle.signal.call_args
        assert "args" in call_kwargs.kwargs
        args = call_kwargs.kwargs["args"]
        assert args[0] == "approve-fix"
        assert args[1] == "approved"
        assert args[3] == "user:james"

    @pytest.mark.asyncio
    async def test_approve_missing_raises_key_error(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """approve() translates RPCError NOT_FOUND to KeyError."""
        from temporalio.service import RPCError

        handle = mock_client.get_workflow_handle.return_value
        handle.signal.side_effect = RPCError("workflow not found", MagicMock(), b"")

        from cloud_agents.workflow.executor.base import ApprovalDecision

        with pytest.raises(KeyError, match="not found"):
            await executor.approve(
                "wf-missing",
                ApprovalDecision(step_name="s1", decision="approved"),
            )


class TestTemporalExecutorCancel:
    """Tests for cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_calls_temporal(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """cancel() calls handle.cancel()."""
        await executor.cancel("wf-1")

        handle = mock_client.get_workflow_handle.return_value
        handle.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_missing_raises_key_error(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """cancel() translates RPCError NOT_FOUND to KeyError."""
        from temporalio.service import RPCError

        handle = mock_client.get_workflow_handle.return_value
        handle.cancel.side_effect = RPCError("workflow not found", MagicMock(), b"")

        with pytest.raises(KeyError, match="not found"):
            await executor.cancel("wf-missing")


class TestTemporalExecutorStatus:
    """Tests for status queries."""

    @pytest.mark.asyncio
    async def test_get_status_queries_workflow(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """get_status() queries the Temporal workflow."""
        handle = mock_client.get_workflow_handle.return_value
        handle.query.return_value = {
            "steps": {"r1": {"status": "completed", "output": {}}},
            "events": [{"type": "step.completed"}],
        }

        status = await executor.get_status("wf-1")
        assert status.workflow_id == "wf-1"
        assert status.status == "running"
        handle.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_status_handles_pydantic_model(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """get_status() handles Pydantic model query results via model_dump()."""
        mock_model = MagicMock()
        mock_model.model_dump.return_value = {
            "steps": {"r1": {"status": "completed"}},
            "events": [],
        }

        handle = mock_client.get_workflow_handle.return_value
        handle.query.return_value = mock_model

        status = await executor.get_status("wf-1")
        assert status.steps == {"r1": {"status": "completed"}}
        mock_model.model_dump.assert_called_once()

    @pytest.mark.asyncio
    async def test_paused_not_overriding_completed(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """Completed workflow with recent pause event stays completed."""
        handle = mock_client.get_workflow_handle.return_value
        handle.query.return_value = {
            "steps": {},
            "events": [{"type": "workflow.paused"}, {"type": "workflow.completed"}],
        }
        handle.describe.return_value = MagicMock(
            status=WorkflowExecutionStatus.COMPLETED
        )

        status = await executor.get_status("wf-1")
        assert status.status == "completed"
        assert status.is_terminal is True

    @pytest.mark.asyncio
    async def test_is_terminal_completed(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """Completed workflow is terminal."""
        handle = mock_client.get_workflow_handle.return_value
        handle.describe.return_value = MagicMock(
            status=WorkflowExecutionStatus.COMPLETED
        )

        assert await executor.is_terminal("wf-1") is True

    @pytest.mark.asyncio
    async def test_is_terminal_timed_out(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """Timed out workflow is terminal."""
        handle = mock_client.get_workflow_handle.return_value
        handle.describe.return_value = MagicMock(
            status=WorkflowExecutionStatus.TIMED_OUT
        )

        assert await executor.is_terminal("wf-1") is True

    @pytest.mark.asyncio
    async def test_is_terminal_running(
        self, executor: Any, mock_client: AsyncMock
    ) -> None:
        """Running workflow is not terminal."""
        assert await executor.is_terminal("wf-1") is False


class TestTemporalExecutorInterface:
    """Verify interface compliance."""

    def test_implements_all_methods(self) -> None:
        """TemporalExecutor implements WorkflowExecutor ABC."""
        from cloud_agents.workflow.executor.base import WorkflowExecutor
        from cloud_agents.workflow.executor.temporal.executor import TemporalExecutor

        assert issubclass(TemporalExecutor, WorkflowExecutor)

    def test_has_temporalio_imports(self) -> None:
        """TemporalExecutor SHOULD have temporalio imports."""
        from cloud_agents.workflow.executor.temporal import executor as mod

        source = open(mod.__file__).read()
        assert "temporalio" in source
