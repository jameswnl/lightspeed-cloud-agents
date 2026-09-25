"""Unit tests for unified retry semantics (issue #268).

max_retries counts retries after the initial attempt: 0 means one total
attempt. Only explicitly retryable transient failures may be retried;
side-effecting tool calls, approval denials, validation failures, and
policy denials are never retried. Both runners (local pydantic-graph and
Temporal) share the classifier and attempt helper below so one-step and
multi-step steps behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from cloud_agents.spawner.base import SpawnConfig
from cloud_agents.workflow.core.definition import WorkflowStepSpec
from cloud_agents.workflow.core.execution import (
    is_transient_failure,
    max_attempts_for,
    run_with_retries,
)


@dataclass
class FakeResult:
    """Minimal stand-in for executor StepResult (status + error)."""

    status: str
    error: Optional[str] = None


def _succeeded(result: FakeResult) -> bool:
    return result.status == "completed"


def _error_of(result: FakeResult) -> Optional[str]:
    return result.error


class TestMaxRetriesModel:
    """Tests for the max_retries contract on the definition model."""

    def _base_step(self, **overrides) -> dict:
        base = {"name": "a", "type": "agent", "prompt": "hi", "output_key": "o"}
        base.update(overrides)
        return base

    def test_default_is_zero_total_one_attempt(self) -> None:
        """Test that omitted max_retries means no retries."""
        step = WorkflowStepSpec.model_validate(self._base_step())
        assert step.max_retries == 0
        assert max_attempts_for(step.max_retries) == 1

    def test_negative_rejected(self) -> None:
        """Test that negative max_retries is rejected."""
        with pytest.raises(Exception):
            WorkflowStepSpec.model_validate(self._base_step(max_retries=-1))

    def test_explicit_value_preserved(self) -> None:
        """Test that explicit max_retries maps to retries + initial."""
        step = WorkflowStepSpec.model_validate(self._base_step(max_retries=2))
        assert max_attempts_for(step.max_retries) == 3


class TestIsTransientFailure:
    """Tests for the transient-failure classifier."""

    @pytest.mark.parametrize(
        "error",
        [
            "spawn failed: pod readiness timeout",
            "connection reset by peer",
            "LLM request timed out after 600s",
            "rate limit exceeded (429)",
            "model unavailable (503)",
            "sandbox heartbeat lost",
            "upstream 502 bad gateway",
        ],
    )
    def test_transient_failures_retry(self, error: str) -> None:
        """Test that infrastructure errors classify as transient."""
        assert is_transient_failure(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "human approval denied",
            "policy denies tool run_fix",
            "output validation failed: missing required key",
            "unauthorized: bad credentials (401)",
            "forbidden by admission policy (403)",
            "agent returned no output",
            "tool kubectl_get failed: connection refused but policy denied retry",
        ],
    )
    def test_non_retryable_failures_do_not_retry(self, error: str) -> None:
        """Test that policy/approval/auth failures never classify transient."""
        assert is_transient_failure(error) is False

    def test_none_and_empty_are_not_transient(self) -> None:
        """Test that missing errors are not retried."""
        assert is_transient_failure(None) is False
        assert is_transient_failure("") is False


class TestRunWithRetries:
    """Tests for the shared attempt helper used by both runners."""

    async def test_success_first_try_single_call(self) -> None:
        """Test that success returns immediately after one attempt."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            return FakeResult(status="completed")

        result = await run_with_retries(attempt, 2, _succeeded, _error_of)
        assert result.status == "completed"
        assert calls == 1

    async def test_transient_failure_then_success(self) -> None:
        """Test that a transient failure is retried and success returned."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeResult(status="failed", error="connection reset")
            return FakeResult(status="completed")

        result = await run_with_retries(attempt, 2, _succeeded, _error_of)
        assert result.status == "completed"
        assert calls == 2

    async def test_persistent_transient_exhausts_attempts(self) -> None:
        """Test that persistent transient failure stops after max+1."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            return FakeResult(status="failed", error="spawn failed: timeout")

        result = await run_with_retries(attempt, 2, _succeeded, _error_of)
        assert result.status == "failed"
        assert calls == 3

    async def test_non_transient_failure_no_retry(self) -> None:
        """Test that policy/approval failures return after one attempt."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            return FakeResult(status="failed", error="human approval denied")

        result = await run_with_retries(attempt, 3, _succeeded, _error_of)
        assert result.status == "failed"
        assert calls == 1

    async def test_transient_exception_retried(self) -> None:
        """Test that transient exceptions are retried."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("connection reset by peer")
            return FakeResult(status="completed")

        result = await run_with_retries(attempt, 2, _succeeded, _error_of)
        assert result.status == "completed"
        assert calls == 2

    async def test_non_transient_exception_raised_immediately(self) -> None:
        """Test that non-transient exceptions propagate without retry."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            raise PermissionError("policy denies tool run_fix")

        with pytest.raises(PermissionError):
            await run_with_retries(attempt, 3, _succeeded, _error_of)
        assert calls == 1

    async def test_exhausted_transient_exceptions_raise_last(self) -> None:
        """Test that exhausted retries re-raise the last exception."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            raise TimeoutError("LLM request timed out")

        with pytest.raises(TimeoutError):
            await run_with_retries(attempt, 1, _succeeded, _error_of)
        assert calls == 2

    async def test_zero_retries_single_attempt(self) -> None:
        """Test that max_retries=0 attempts exactly once."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            return FakeResult(status="failed", error="connection reset")

        result = await run_with_retries(attempt, 0, _succeeded, _error_of)
        assert calls == 1
        assert result.status == "failed"


class TestSpawnConfigSandboxImage:
    """Tests for the spawn_config sandbox image override (issue #268)."""

    def test_sandbox_image_defaults_none(self) -> None:
        """Test that no image override is the default."""
        assert SpawnConfig().sandbox_image is None

    def test_sandbox_image_accepts_reference(self) -> None:
        """Test that an OCI reference validates."""
        config = SpawnConfig(sandbox_image="quay.io/example/sandbox:v1")
        assert config.sandbox_image == "quay.io/example/sandbox:v1"


class TestOutputSchemaFailuresDoNotRetry:
    """Schema-validation failures are terminal, not transient (issue #268)."""

    def test_schema_failure_text_is_not_transient(self) -> None:
        """Test the executor's schema-failure message never retries."""
        assert (
            is_transient_failure(
                "LLM returned non-JSON response but output_schema was requested: ..."
            )
            is False
        )
        assert (
            is_transient_failure(
                "LLM returned null content but output_schema was requested"
            )
            is False
        )

    async def test_schema_failure_attempts_once(self) -> None:
        """Test that a schema failure returns after one attempt."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            return FakeResult(
                status="failed",
                error="LLM returned non-JSON response but output_schema was requested",
            )

        result = await run_with_retries(attempt, 3, _succeeded, _error_of)
        assert result.status == "failed"
        assert calls == 1


class TestActivityErrorUnwrap:
    """ActivityError wrappers classify by root cause (issue #268, B5)."""

    def test_unwraps_cause_chain(self) -> None:
        """Test that chained causes surface for classification."""
        from cloud_agents.workflow.core.execution import activity_error_text

        root = RuntimeError("upstream 502 bad gateway")
        mid = ConnectionError("activity failed")
        mid.__cause__ = root
        outer = TimeoutError("wrapper")
        outer.__cause__ = mid
        text = activity_error_text(outer)
        assert "502" in text
        assert is_transient_failure(text) is True

    def test_bare_wrapper_without_cause(self) -> None:
        """Test that a causeless wrapper yields its own text."""
        from cloud_agents.workflow.core.execution import activity_error_text

        text = activity_error_text(RuntimeError("boom"))
        assert text == "boom"

    async def test_transient_cause_retries_with_unwrapper(self) -> None:
        """Test run_with_retries honors exc_text for wrapped failures."""
        from cloud_agents.workflow.core.execution import activity_error_text

        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                wrapper = RuntimeError("ActivityError(run_sandbox_step)")
                wrapper.__cause__ = ConnectionError("connection reset by peer")
                raise wrapper
            return FakeResult(status="completed")

        result = await run_with_retries(
            attempt, 1, _succeeded, _error_of, exc_text=activity_error_text
        )
        assert result.status == "completed"
        assert calls == 2

    async def test_non_transient_cause_raises_immediately(self) -> None:
        """Test a policy-denial cause is not retried through the wrapper."""
        from cloud_agents.workflow.core.execution import activity_error_text

        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            wrapper = RuntimeError("ActivityError(run_sandbox_step)")
            wrapper.__cause__ = PermissionError("policy denies tool run_fix")
            raise wrapper

        with pytest.raises(RuntimeError):
            await run_with_retries(
                attempt, 3, _succeeded, _error_of, exc_text=activity_error_text
            )
        assert calls == 1


class TestToolFailuresDoNotRetry:
    """Tool-execution failures are terminal without an idempotency contract (S1)."""

    def test_tool_failure_with_infra_text_is_not_transient(self) -> None:
        """Test that a tool failure mentioning infra still does not retry."""
        assert is_transient_failure("tool kubectl_get failed: connection reset") is False
        assert is_transient_failure("tool run_fix timed out after 30s") is False
        assert is_transient_failure("tool exec error: 502 bad gateway") is False

    def test_plain_infra_failure_still_retries(self) -> None:
        """Test that non-tool infra failures remain retryable."""
        assert is_transient_failure("connection reset by peer") is True
        assert is_transient_failure("upstream 502 bad gateway") is True

    async def test_tool_failure_attempts_once(self) -> None:
        """Test that a tool failure returns after one attempt."""
        calls = 0

        async def attempt() -> FakeResult:
            nonlocal calls
            calls += 1
            return FakeResult(
                status="failed", error="tool kubectl_get failed: timeout"
            )

        result = await run_with_retries(attempt, 3, _succeeded, _error_of)
        assert result.status == "failed"
        assert calls == 1


class TestChunkParallelGroups:
    """Contiguous same-group steps chunk together, in order (S6)."""

    def test_sequential_steps_chunk_singly(self) -> None:
        """Test that ungrouped steps form singleton chunks."""
        from cloud_agents.workflow.core.execution import chunk_parallel_groups

        chunks = chunk_parallel_groups([{"name": "a"}, {"name": "b"}])
        assert chunks == [(None, [{"name": "a"}]), (None, [{"name": "b"}])]

    def test_contiguous_group_chunks_together(self) -> None:
        """Test that a contiguous group forms one unit."""
        from cloud_agents.workflow.core.execution import chunk_parallel_groups

        steps = [
            {"name": "a"},
            {"name": "b", "parallel_group": "g"},
            {"name": "c", "parallel_group": "g"},
            {"name": "d"},
        ]
        chunks = chunk_parallel_groups(steps)
        assert [group for group, _ in chunks] == [None, "g", None]
        assert [s["name"] for s in chunks[1][1]] == ["b", "c"]

    def test_regrouped_name_starts_new_unit(self) -> None:
        """Test that a later reuse of a group name starts a new unit."""
        from cloud_agents.workflow.core.execution import chunk_parallel_groups

        steps = [
            {"name": "a", "parallel_group": "g"},
            {"name": "b"},
            {"name": "c", "parallel_group": "g"},
        ]
        chunks = chunk_parallel_groups(steps)
        assert [group for group, _ in chunks] == ["g", None, "g"]

    def test_empty_steps(self) -> None:
        """Test that no steps yield no chunks."""
        from cloud_agents.workflow.core.execution import chunk_parallel_groups

        assert chunk_parallel_groups([]) == []
