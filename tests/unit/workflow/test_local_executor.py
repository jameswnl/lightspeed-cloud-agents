"""Tests for the LocalWorkflowRunner implementing WorkflowRunner interface."""

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
    """Create a LocalWorkflowRunner with mocked dependencies."""
    from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

    return LocalWorkflowRunner(spawner=mock_spawner, run_state_store=mock_store)


class TestLocalWorkflowRunnerStart:
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


class TestLocalWorkflowRunnerStatus:
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


class TestLocalWorkflowRunnerApproval:
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


class TestLocalWorkflowRunnerCancel:
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


class TestLocalWorkflowRunnerIsTerminal:
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


class TestLocalWorkflowRunnerSendMessage:
    """Tests for send_message/send_message_stream defaults."""

    @pytest.mark.asyncio
    async def test_send_message_raises_not_implemented(
        self, executor: Any
    ) -> None:
        """send_message() raises NotImplementedError on predefined runners."""
        with pytest.raises(NotImplementedError, match="does not support interactive messages"):
            await executor.send_message("wf-1", "hello")

    @pytest.mark.asyncio
    async def test_send_message_stream_raises_not_implemented(
        self, executor: Any
    ) -> None:
        """send_message_stream() raises NotImplementedError on predefined runners."""
        with pytest.raises(NotImplementedError, match="does not support interactive streaming"):
            async for _ in executor.send_message_stream("wf-1", "hello"):
                pass


class TestLocalWorkflowRunnerNoTemporal:
    """Verify no Temporal imports."""

    def test_no_temporal_imports(self) -> None:
        """LocalWorkflowRunner module has zero temporalio imports."""
        from cloud_agents.workflow.executor.local import executor as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source


class TestLocalWorkflowRunnerTraceContinuity:
    """Tests for trace_parent persistence on pause and resume passthrough (issue #179)."""

    @pytest.mark.asyncio
    async def test_pause_merges_trace_parent_into_existing_context(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """On pause, trace_parent is merged into workflow_context, not overwritten."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        mock_store.get.return_value = {
            "workflow_context": {"sandbox_image": "custom:latest", "mcp_servers": ["x"]},
        }
        mock_store.update_workflow_context = AsyncMock()

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"name": "approve", "type": "human-approval", "output_key": "approval"},
                ]
            },
        }
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )
        state.trace_parent = "00-cccc-dddd-01"

        await executor._execute("wf-1", graph, state)

        mock_store.update_workflow_context.assert_called_once()
        call_args = mock_store.update_workflow_context.call_args[0]
        assert call_args[0] == "wf-1"
        merged = call_args[1]
        assert merged["trace_parent"] == "00-cccc-dddd-01"
        assert merged["sandbox_image"] == "custom:latest"
        assert merged["mcp_servers"] == ["x"]

    @pytest.mark.asyncio
    async def test_pause_without_trace_parent_skips_context_update(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """No trace_parent captured -- no workflow_context write on pause."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        mock_store.update_workflow_context = AsyncMock()

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"name": "approve", "type": "human-approval", "output_key": "approval"},
                ]
            },
        }
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await executor._execute("wf-1", graph, state)

        mock_store.update_workflow_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_still_happens_when_trace_parent_persist_fails(
        self, executor: Any, mock_store: AsyncMock
    ) -> None:
        """A failure persisting trace_parent must not fail the whole workflow.

        Pausing for approval is the primary outcome; tracing continuity is
        best-effort and must not turn a pause into a failure.
        """
        from cloud_agents.workflow.executor.graph_translator import build_graph

        mock_store.get.side_effect = RuntimeError("DB connection lost")

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"name": "approve", "type": "human-approval", "output_key": "approval"},
                ]
            },
        }
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )
        state.trace_parent = "00-cccc-dddd-01"

        await executor._execute("wf-1", graph, state)

        mock_store.set_paused.assert_called_once_with("wf-1", "approve")
        mock_store.mark_terminal.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_passes_identity_and_seeds_resume_trace_parent(
        self, executor: Any, mock_store: AsyncMock, mocker: MockerFixture
    ) -> None:
        """_resume_from_store forwards user_id/session_id and seeds resume_trace_parent."""
        from cloud_agents.workflow.executor.graph_translator import WorkflowGraphState

        mock_store.get.return_value = {
            "definition": {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "test"},
                "spec": {
                    "steps": [
                        {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
                    ]
                },
            },
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "workflow_context": {"trace_parent": "00-eeee-ffff-01"},
            "user_id": "jwong",
            "session_id": "ses-abc",
            "steps": {},
            "status": "paused",
        }

        captured_kwargs: dict[str, Any] = {}
        fake_state = WorkflowGraphState()

        def fake_build_graph(definition: Any, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return mocker.MagicMock(), fake_state

        mocker.patch(
            "cloud_agents.workflow.executor.local.executor.build_graph",
            side_effect=fake_build_graph,
        )
        # Prevent the background _execute() task from actually running --
        # this test only cares about state passed into build_graph/graph_state.
        mocker.patch(
            "cloud_agents.workflow.executor.local.executor.asyncio.create_task",
            return_value=mocker.MagicMock(),
        )

        await executor._resume_from_store("wf-1")

        assert captured_kwargs["user_id"] == "jwong"
        assert captured_kwargs["session_id"] == "ses-abc"
        assert fake_state.resume_trace_parent == "00-eeee-ffff-01"

    @pytest.mark.asyncio
    async def test_resume_without_stored_trace_parent_leaves_it_unset(
        self, executor: Any, mock_store: AsyncMock, mocker: MockerFixture
    ) -> None:
        """No trace_parent in workflow_context -> resume_trace_parent stays None."""
        from cloud_agents.workflow.executor.graph_translator import WorkflowGraphState

        mock_store.get.return_value = {
            "definition": {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "test"},
                "spec": {
                    "steps": [
                        {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
                    ]
                },
            },
            "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            "workflow_context": {},
            "user_id": None,
            "session_id": None,
            "steps": {},
            "status": "paused",
        }

        fake_state = WorkflowGraphState()

        mocker.patch(
            "cloud_agents.workflow.executor.local.executor.build_graph",
            return_value=(mocker.MagicMock(), fake_state),
        )
        mocker.patch(
            "cloud_agents.workflow.executor.local.executor.asyncio.create_task",
            return_value=mocker.MagicMock(),
        )

        await executor._resume_from_store("wf-1")

        assert fake_state.resume_trace_parent is None


class TestLocalWorkflowRunnerLiveTraceSharing:
    """Tests that a live (non-paused) run's steps share one trace_id (#183).

    Uses a real OTEL SDK TracerProvider (isolated to this test via
    monkeypatching the module-level _tracer singletons, not the global
    provider) since NoOp spans have no meaningful trace_id to compare.
    """

    @pytest.mark.asyncio
    async def test_steps_share_one_trace_id(
        self,
        executor: Any,
        mock_spawner: AsyncMock,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Two sequential agent steps in one live run produce one shared trace_id."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from cloud_agents.workflow.executor.step.base import StepResult

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        mocker.patch(
            "cloud_agents.workflow.executor.local.executor._tracer",
            provider.get_tracer("test-local-executor"),
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator._tracer",
            provider.get_tracer("test-graph-translator"),
        )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_input([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
            {"name": "s2", "type": "agent", "prompt": "test2", "output_key": "r2"},
        ])

        workflow_id = await executor.start(input_data)
        await executor._running[workflow_id]

        spans = exporter.get_finished_spans()
        step_spans = [s for s in spans if s.name == "step.execute"]
        workflow_spans = [s for s in spans if s.name == "workflow.execute"]

        assert len(step_spans) == 2
        assert len(workflow_spans) == 1

        trace_ids = {s.context.trace_id for s in step_spans}
        assert trace_ids == {workflow_spans[0].context.trace_id}

    @pytest.mark.asyncio
    async def test_workflow_span_has_workflow_id_attribute(
        self,
        executor: Any,
        mock_spawner: AsyncMock,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """The new workflow.execute span carries the workflow.id attribute."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from cloud_agents.workflow.executor.step.base import StepResult

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        mocker.patch(
            "cloud_agents.workflow.executor.local.executor._tracer",
            provider.get_tracer("test-local-executor"),
        )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_input([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
        ])
        input_data["session_id"] = "ses-abc"

        workflow_id = await executor.start(input_data)
        await executor._running[workflow_id]

        spans = exporter.get_finished_spans()
        workflow_span = next(s for s in spans if s.name == "workflow.execute")

        assert workflow_span.attributes["workflow.id"] == workflow_id
        assert workflow_span.attributes["session.id"] == "ses-abc"

    @pytest.mark.asyncio
    async def test_resumed_segment_gets_a_separate_trace_from_pre_pause_segment(
        self,
        executor: Any,
        mock_spawner: AsyncMock,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """A resume must NOT unify pre-pause and post-resume steps into one
        trace -- #179/#181 deliberately chose two linked traces over a
        single waterfall spanning the pause. This guards that this fix
        for the live-run case didn't accidentally undo that.
        """
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from cloud_agents.workflow.executor.base import ApprovalDecision
        from cloud_agents.workflow.executor.step.base import StepResult

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        mocker.patch(
            "cloud_agents.workflow.executor.local.executor._tracer",
            provider.get_tracer("test-local-executor"),
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator._tracer",
            provider.get_tracer("test-graph-translator"),
        )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        definition = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
                    {"name": "approve-fix", "type": "human-approval", "output_key": "approval"},
                    {"name": "s2", "type": "agent", "prompt": "test2", "output_key": "r2"},
                ]
            },
        }
        provider_cfg = {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"}

        workflow_id = await executor.start(
            {"definition": definition, "provider": provider_cfg}
        )
        await executor._running[workflow_id]

        mock_store.get.return_value = {
            "workflow_id": workflow_id,
            "status": "paused",
            "current_step": "approve-fix",
            "steps": {"r1": {"status": "completed", "output": {"ok": True}}},
            "definition": definition,
            "provider": provider_cfg,
            "workflow_context": {
                "trace_parent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
            },
            "user_id": None,
            "session_id": None,
        }

        await executor.approve(
            workflow_id, ApprovalDecision(step_name="approve-fix", decision="approved")
        )
        await executor._running[workflow_id]

        workflow_spans = [
            s for s in exporter.get_finished_spans() if s.name == "workflow.execute"
        ]
        assert len(workflow_spans) == 2
        trace_ids = {s.context.trace_id for s in workflow_spans}
        assert len(trace_ids) == 2
