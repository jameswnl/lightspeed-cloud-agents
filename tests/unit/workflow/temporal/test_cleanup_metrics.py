"""Unit tests for cleanup failure and orphan metrics (T3)."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY
from pytest_mock import MockerFixture

from cloud_agents.workflow.temporal_activities import run_sandbox_step


def _get_counter_value(name: str, labels: dict | None = None) -> float:
    """Read a Prometheus counter's current value."""
    for metric in REGISTRY.collect():
        if metric.name == name:
            for sample in metric.samples:
                if sample.name == f"{name}_total":
                    if labels is None or all(sample.labels.get(k) == v for k, v in labels.items()):
                        return sample.value
    return 0.0


class TestCleanupFailureMetrics:
    """Tests for ls_sandbox_cleanup_failures_total counter."""

    @pytest.mark.asyncio
    async def test_cleanup_failure_increments_counter(self, mocker: MockerFixture) -> None:
        """Failed spawner.destroy() increments cleanup failure counter."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True
        mock_spawner.destroy.side_effect = RuntimeError("destroy failed")

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch("cloud_agents.workflow.temporal_activities.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(post=mocker.AsyncMock(return_value=mock_response)),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)


        before = _get_counter_value("ls_sandbox_cleanup_failures", {"step_name": "fail-step"})

        await run_sandbox_step({
            "step": {"name": "fail-step", "prompt": "test", "output_key": "r1"},
            "workflow_id": "wf-cleanup-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        after = _get_counter_value("ls_sandbox_cleanup_failures", {"step_name": "fail-step"})
        assert after > before

    @pytest.mark.asyncio
    async def test_successful_cleanup_does_not_increment(self, mocker: MockerFixture) -> None:
        """Successful spawner.destroy() does not increment failure counter."""
        mock_spawner = mocker.AsyncMock()
        mock_spawner.spawn.return_value = "http://pod-1:8080"
        mock_spawner.wait_ready.return_value = True

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {}}

        mock_http = mocker.patch("cloud_agents.workflow.temporal_activities.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(
            return_value=mocker.MagicMock(post=mocker.AsyncMock(return_value=mock_response)),
        )
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)


        before = _get_counter_value("ls_sandbox_cleanup_failures", {"step_name": "ok-step"})

        await run_sandbox_step({
            "step": {"name": "ok-step", "prompt": "test", "output_key": "r1"},
            "workflow_id": "wf-cleanup-2",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        }, spawner=mock_spawner)

        after = _get_counter_value("ls_sandbox_cleanup_failures", {"step_name": "ok-step"})
        assert after == before


class TestOrphanCleanupMetrics:
    """Tests for ls_sandbox_orphans_cleaned_total counter."""

    @pytest.mark.asyncio
    async def test_orphan_cleanup_increments_counter(self, mocker: MockerFixture) -> None:
        """Orphan reconciliation increments orphan cleanup counter."""
        from cloud_agents.workflow.temporal_entrypoint import reconcile_orphaned_sandboxes

        mock_spawner = mocker.AsyncMock()
        mock_spawner.list_active.return_value = ["orphan-1", "orphan-2"]

        before = _get_counter_value("ls_sandbox_orphans_cleaned")

        await reconcile_orphaned_sandboxes(mock_spawner)

        after = _get_counter_value("ls_sandbox_orphans_cleaned")
        assert after >= before + 2

    @pytest.mark.asyncio
    async def test_no_orphans_no_increment(self, mocker: MockerFixture) -> None:
        """No orphans means no counter increment."""
        from cloud_agents.workflow.temporal_entrypoint import reconcile_orphaned_sandboxes

        mock_spawner = mocker.AsyncMock()
        mock_spawner.list_active.return_value = []

        before = _get_counter_value("ls_sandbox_orphans_cleaned")

        await reconcile_orphaned_sandboxes(mock_spawner)

        after = _get_counter_value("ls_sandbox_orphans_cleaned")
        assert after == before

    @pytest.mark.asyncio
    async def test_partial_destroy_failure_counts_only_successes(self, mocker: MockerFixture) -> None:
        """Only successfully destroyed orphans are counted in the metric."""
        from cloud_agents.workflow.temporal_entrypoint import reconcile_orphaned_sandboxes

        mock_spawner = mocker.AsyncMock()
        mock_spawner.list_active.return_value = ["orphan-ok", "orphan-fail", "orphan-ok2"]

        call_count = 0

        async def destroy_side_effect(name: str) -> None:
            nonlocal call_count
            call_count += 1
            if name == "orphan-fail":
                raise RuntimeError("destroy failed")

        mock_spawner.destroy.side_effect = destroy_side_effect

        before = _get_counter_value("ls_sandbox_orphans_cleaned")

        await reconcile_orphaned_sandboxes(mock_spawner)

        after = _get_counter_value("ls_sandbox_orphans_cleaned")
        assert after >= before + 2  # 2 succeeded, 1 failed
        assert after < before + 3   # NOT 3 — the failed one shouldn't count


class TestSandboxTimeoutMetrics:
    """Tests for ls_sandbox_timeout_total counter (T2)."""

    def test_counter_increments_with_timeout_reason(self) -> None:
        """ls_sandbox_timeout_total increments with reason=timeout."""
        from cloud_agents.workflow.temporal_metrics import ls_sandbox_timeout_total

        before = _get_counter_value(
            "ls_sandbox_timeout", {"step_name": "t2-step", "reason": "timeout"}
        )
        ls_sandbox_timeout_total.labels(step_name="t2-step", reason="timeout").inc()
        after = _get_counter_value(
            "ls_sandbox_timeout", {"step_name": "t2-step", "reason": "timeout"}
        )
        assert after > before

    def test_counter_increments_with_cancelled_reason(self) -> None:
        """ls_sandbox_timeout_total increments with reason=cancelled."""
        from cloud_agents.workflow.temporal_metrics import ls_sandbox_timeout_total

        before = _get_counter_value(
            "ls_sandbox_timeout", {"step_name": "t2-step", "reason": "cancelled"}
        )
        ls_sandbox_timeout_total.labels(step_name="t2-step", reason="cancelled").inc()
        after = _get_counter_value(
            "ls_sandbox_timeout", {"step_name": "t2-step", "reason": "cancelled"}
        )
        assert after > before
