"""Prometheus metrics for Temporal workflow execution.

Follows the ls_* naming convention from src/agents/runtime/metrics.py.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

ls_workflow_runs_total = Counter(
    "ls_workflow_runs_total",
    "Total number of workflow executions",
    ["workflow_name", "status"],
)

ls_workflow_run_duration_seconds = Histogram(
    "ls_workflow_run_duration_seconds",
    "Duration of workflow executions in seconds",
    ["workflow_name"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800),
)

ls_workflow_step_runs_total = Counter(
    "ls_workflow_step_runs_total",
    "Total number of workflow step executions",
    ["step_name", "status"],
)

ls_workflow_step_duration_seconds = Histogram(
    "ls_workflow_step_duration_seconds",
    "Duration of workflow step executions in seconds",
    ["step_name"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)
