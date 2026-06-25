"""Unit tests for Phase 7 robustness patterns."""

from __future__ import annotations

import hashlib
import tempfile

import pytest

from agents.workflow.persistence import FilePersistence
from agents.workflow.state import StepResult, WorkflowState


class TestContentHashNaming:
    """Tests for content-hash based spawn names."""

    def test_same_input_same_hash(self) -> None:
        """Test that identical inputs produce identical spawn names."""
        def make_name(wf_id: str, step: str, attempt: int) -> str:
            hash_input = f"{wf_id}:{step}:{attempt}"
            return hashlib.sha256(hash_input.encode()).hexdigest()[:8]

        name1 = make_name("wf-123", "diagnose", 1)
        name2 = make_name("wf-123", "diagnose", 1)
        assert name1 == name2

    def test_different_workflow_different_hash(self) -> None:
        """Test that different workflow IDs produce different names."""
        def make_name(wf_id: str, step: str, attempt: int) -> str:
            hash_input = f"{wf_id}:{step}:{attempt}"
            return hashlib.sha256(hash_input.encode()).hexdigest()[:8]

        name1 = make_name("wf-111", "diagnose", 1)
        name2 = make_name("wf-222", "diagnose", 1)
        assert name1 != name2

    def test_different_step_different_hash(self) -> None:
        """Test that different step names produce different names."""
        def make_name(wf_id: str, step: str, attempt: int) -> str:
            hash_input = f"{wf_id}:{step}:{attempt}"
            return hashlib.sha256(hash_input.encode()).hexdigest()[:8]

        name1 = make_name("wf-123", "diagnose", 1)
        name2 = make_name("wf-123", "fix", 1)
        assert name1 != name2

    def test_different_attempt_different_hash(self) -> None:
        """Test that different attempt numbers produce different names."""
        def make_name(wf_id: str, step: str, attempt: int) -> str:
            hash_input = f"{wf_id}:{step}:{attempt}"
            return hashlib.sha256(hash_input.encode()).hexdigest()[:8]

        name1 = make_name("wf-123", "diagnose", 1)
        name2 = make_name("wf-123", "diagnose", 2)
        assert name1 != name2


class TestDeriveStatus:
    """Tests for WorkflowState.derive_status()."""

    def test_empty_steps_is_running(self) -> None:
        """Test that no steps means running."""
        assert WorkflowState.derive_status({}) == "running"

    def test_all_completed_is_completed(self) -> None:
        """Test that all completed steps → completed."""
        steps = {
            "s1": StepResult(step_name="s1", status="completed"),
            "s2": StepResult(step_name="s2", status="completed"),
        }
        assert WorkflowState.derive_status(steps) == "completed"

    def test_any_failed_is_failed(self) -> None:
        """Test that any failed step → failed."""
        steps = {
            "s1": StepResult(step_name="s1", status="completed"),
            "s2": StepResult(step_name="s2", status="failed"),
        }
        assert WorkflowState.derive_status(steps) == "failed"

    def test_awaiting_approval_is_paused(self) -> None:
        """Test that awaiting_approval → paused."""
        steps = {
            "s1": StepResult(step_name="s1", status="completed"),
            "s2": StepResult(step_name="s2", status="awaiting_approval"),
        }
        assert WorkflowState.derive_status(steps) == "paused"

    def test_dispatched_is_running(self) -> None:
        """Test that dispatched step → running."""
        steps = {
            "s1": StepResult(step_name="s1", status="dispatched"),
        }
        assert WorkflowState.derive_status(steps) == "running"

    def test_mixed_completed_skipped_is_completed(self) -> None:
        """Test that completed + skipped → completed."""
        steps = {
            "s1": StepResult(step_name="s1", status="completed"),
            "s2": StepResult(step_name="s2", status="skipped"),
        }
        assert WorkflowState.derive_status(steps) == "completed"


class TestFilePersistenceCAS:
    """Tests for FilePersistence.save_cas()."""

    @pytest.mark.asyncio
    async def test_cas_success(self) -> None:
        """Test that CAS succeeds with correct version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = FilePersistence(tmpdir)
            state = WorkflowState(
                workflow_id="fp-1", workflow_name="test",
                created_at="2026-01-01", updated_at="2026-01-01", version=1,
            )
            await p.save(state)

            ok = await p.save_cas(state, expected_version=1)
            assert ok is True
            assert state.version == 2

    @pytest.mark.asyncio
    async def test_cas_rejects_stale(self) -> None:
        """Test that CAS rejects stale version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = FilePersistence(tmpdir)
            state = WorkflowState(
                workflow_id="fp-2", workflow_name="test",
                created_at="2026-01-01", updated_at="2026-01-01", version=3,
            )
            await p.save(state)

            ok = await p.save_cas(state, expected_version=1)
            assert ok is False

    @pytest.mark.asyncio
    async def test_cas_new_file(self) -> None:
        """Test CAS on a file that doesn't exist yet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = FilePersistence(tmpdir)
            state = WorkflowState(
                workflow_id="fp-3", workflow_name="test",
                created_at="2026-01-01", updated_at="2026-01-01", version=1,
            )
            ok = await p.save_cas(state, expected_version=1)
            assert ok is True
            assert state.version == 2

            loaded = await p.load("fp-3")
            assert loaded is not None
            assert loaded.version == 2


class TestDefinitionSnapshotOnAllRuns:
    """Tests for definition_snapshot being set on all workflow runs."""

    @pytest.mark.asyncio
    async def test_snapshot_set_on_run(self) -> None:
        """Test that definition_snapshot is populated when workflow starts."""
        from unittest.mock import AsyncMock
        from agents.models import AgentRunResponse
        from agents.registry import AgentRegistry
        from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
        from agents.workflow.executor import WorkflowExecutor

        defn = WorkflowDefinition(
            apiVersion="v1", kind="AgentWorkflow",
            metadata={"name": "snap-test"},
            spec=WorkflowSpec(steps=[
                WorkflowStepSpec(name="s1", type="agent", agent="diag",
                                 prompt="test", output_key="r1", spawn="pre-deployed"),
            ]),
        )
        client = AsyncMock()
        client.run = AsyncMock(return_value=AgentRunResponse(
            output={"ok": True}, output_type="str",
            usage={"input_tokens": 1, "output_tokens": 1},
            agent_name="diag", success=True,
        ))
        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)

        state = await executor.run()
        assert state.definition_snapshot is not None
        assert state.definition_snapshot["metadata"]["name"] == "snap-test"

    @pytest.mark.asyncio
    async def test_snapshot_survives_persistence_roundtrip(self) -> None:
        """Test that definition_snapshot survives save+load."""
        from agents.workflow.persistence import InMemoryPersistence

        p = InMemoryPersistence()
        state = WorkflowState(
            workflow_id="snap-rt", workflow_name="test",
            definition_snapshot={"apiVersion": "v1", "metadata": {"name": "test"}},
            created_at="2026-01-01", updated_at="2026-01-01",
        )
        await p.save(state)
        loaded = await p.load("snap-rt")
        assert loaded.definition_snapshot is not None
        assert loaded.definition_snapshot["metadata"]["name"] == "test"


class TestPermissionScopeEnforcement:
    """Tests that PermissionScope actually filters tools at runtime."""

    @pytest.mark.asyncio
    async def test_denied_tool_not_available(self) -> None:
        """Test that a denied tool is excluded from the agent's tool set."""
        from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
        from pydantic_ai.models.function import AgentInfo, FunctionModel
        from agents.definition import AgentSpec, LifecycleSpec, ToolsSpec
        from agents.models import AgentRunRequest, DiagnosticReport
        from agents.runtime.generic_runner import create_generic_runner
        from examples.agents.diagnostic.cluster_state import init_scenario

        init_scenario("healthy")

        tool_names_seen = []

        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_names_seen.extend(t.name for t in info.function_tools)
            report = DiagnosticReport(
                summary="ok", issues_found=[], actions_taken=[], cluster_healthy=True,
            )
            return ModelResponse(parts=[TextPart(content=report.model_dump_json())])

        spec = AgentSpec(
            instructions="Test agent",
            output_type="DiagnosticReport",
            tools=ToolsSpec(
                module="examples.agents.diagnostic.tools",
                functions=["list_hosts", "check_host", "run_remediation"],
            ),
            lifecycle=LifecycleSpec(type="request-response"),
        )
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        request = AgentRunRequest(
            prompt="check",
            context={"denied_tools": ["run_remediation"]},
        )
        await runner(request)

        assert "list_hosts" in tool_names_seen
        assert "check_host" in tool_names_seen
        assert "run_remediation" not in tool_names_seen

    @pytest.mark.asyncio
    async def test_allowed_tools_whitelist(self) -> None:
        """Test that only allowed tools are available."""
        from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
        from pydantic_ai.models.function import AgentInfo, FunctionModel
        from agents.definition import AgentSpec, LifecycleSpec, ToolsSpec
        from agents.models import AgentRunRequest, DiagnosticReport
        from agents.runtime.generic_runner import create_generic_runner
        from examples.agents.diagnostic.cluster_state import init_scenario

        init_scenario("healthy")

        tool_names_seen = []

        def mock_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_names_seen.extend(t.name for t in info.function_tools)
            report = DiagnosticReport(
                summary="ok", issues_found=[], actions_taken=[], cluster_healthy=True,
            )
            return ModelResponse(parts=[TextPart(content=report.model_dump_json())])

        spec = AgentSpec(
            instructions="Test agent",
            output_type="DiagnosticReport",
            tools=ToolsSpec(
                module="examples.agents.diagnostic.tools",
                functions=["list_hosts", "check_host", "run_remediation"],
            ),
            lifecycle=LifecycleSpec(type="request-response"),
        )
        runner = create_generic_runner(spec, FunctionModel(mock_llm), "test-agent")
        request = AgentRunRequest(
            prompt="check",
            context={"allowed_tools": ["list_hosts"]},
        )
        await runner(request)

        assert "list_hosts" in tool_names_seen
        assert "check_host" not in tool_names_seen
        assert "run_remediation" not in tool_names_seen


class TestRetryEscalationIntegration:
    """Integration test for retry → escalation lifecycle."""

    @pytest.mark.asyncio
    async def test_retry_exhaustion_generates_escalation(self) -> None:
        """Test that failed retries produce escalation with failure history."""
        from unittest.mock import AsyncMock
        from agents.registry import AgentRegistry
        from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
        from agents.workflow.executor import WorkflowExecutor

        defn = WorkflowDefinition(
            apiVersion="v1", kind="AgentWorkflow",
            metadata={"name": "retry-test"},
            spec=WorkflowSpec(steps=[
                WorkflowStepSpec(name="flaky", type="agent", agent="diag",
                                 prompt="fix it", output_key="result",
                                 max_retries=2, spawn="pre-deployed"),
            ]),
        )
        client = AsyncMock()
        client.run = AsyncMock(side_effect=Exception("Agent crashed"))

        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)

        state = await executor.run()

        assert state.status == "failed"
        result = state.steps["result"]
        assert result.status == "failed"
        assert "Retries exhausted" in result.error
        assert result.output is not None
        assert "failure_history" in result.output
        assert len(result.output["failure_history"]) == 2
        assert "Agent crashed" in result.output["failure_history"][0]["error"]
        assert "Agent crashed" in result.output["failure_history"][1]["error"]
        assert client.run.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_context_passed_to_second_attempt(self) -> None:
        """Test that retry prompt includes previous failure context."""
        from unittest.mock import AsyncMock
        from agents.models import AgentRunResponse
        from agents.registry import AgentRegistry
        from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
        from agents.workflow.executor import WorkflowExecutor

        defn = WorkflowDefinition(
            apiVersion="v1", kind="AgentWorkflow",
            metadata={"name": "retry-ctx-test"},
            spec=WorkflowSpec(steps=[
                WorkflowStepSpec(name="flaky", type="agent", agent="diag",
                                 prompt="Original prompt", output_key="result",
                                 max_retries=2, spawn="pre-deployed"),
            ]),
        )
        prompts_seen = []
        call_count = 0

        async def tracking_run(prompt, **kwargs):
            nonlocal call_count
            prompts_seen.append(prompt)
            call_count += 1
            if call_count == 1:
                raise Exception("First failure")
            return AgentRunResponse(
                output={"fixed": True}, output_type="str",
                usage={"input_tokens": 1, "output_tokens": 1},
                agent_name="diag", success=True,
            )

        client = AsyncMock()
        client.run = tracking_run

        registry = AgentRegistry({"diag": "http://diag:8080"})
        executor = WorkflowExecutor(defn, registry, client_factory=lambda _: client)

        state = await executor.run()

        assert state.status == "completed"
        assert len(prompts_seen) == 2
        assert "Original prompt" in prompts_seen[0]
        assert "PREVIOUS ATTEMPTS" in prompts_seen[1]
        assert "First failure" in prompts_seen[1]
