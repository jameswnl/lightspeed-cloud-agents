"""Unit tests for Temporal workflow Prometheus metrics."""

from __future__ import annotations


class TestMetricsExist:
    """Tests that workflow metrics are defined with correct names and labels."""

    def test_workflow_runs_total_counter(self) -> None:
        """ls_workflow_runs_total counter exists with correct labels."""
        from agents.workflow.temporal_metrics import ls_workflow_runs_total
        assert "ls_workflow_runs" in ls_workflow_runs_total._name
        assert "workflow_name" in ls_workflow_runs_total._labelnames
        assert "status" in ls_workflow_runs_total._labelnames

    def test_workflow_run_duration_histogram(self) -> None:
        """ls_workflow_run_duration_seconds histogram exists."""
        from agents.workflow.temporal_metrics import ls_workflow_run_duration_seconds
        assert ls_workflow_run_duration_seconds._name == "ls_workflow_run_duration_seconds"
        assert "workflow_name" in ls_workflow_run_duration_seconds._labelnames

    def test_step_runs_total_counter(self) -> None:
        """ls_workflow_step_runs_total counter exists with correct labels."""
        from agents.workflow.temporal_metrics import ls_workflow_step_runs_total
        assert "ls_workflow_step_runs" in ls_workflow_step_runs_total._name
        assert "step_name" in ls_workflow_step_runs_total._labelnames
        assert "status" in ls_workflow_step_runs_total._labelnames

    def test_step_duration_histogram(self) -> None:
        """ls_workflow_step_duration_seconds histogram exists."""
        from agents.workflow.temporal_metrics import ls_workflow_step_duration_seconds
        assert ls_workflow_step_duration_seconds._name == "ls_workflow_step_duration_seconds"
        assert "step_name" in ls_workflow_step_duration_seconds._labelnames
