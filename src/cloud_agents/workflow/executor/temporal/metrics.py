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

ls_sandbox_cleanup_failures_total = Counter(
    "ls_sandbox_cleanup_failures_total",
    "Number of sandbox containers that failed to be destroyed",
    ["step_name"],
)

ls_sandbox_timeout_total = Counter(
    "ls_sandbox_timeout_total",
    "Number of sandbox activities cancelled or timed out",
    ["step_name", "reason"],
)

ls_sandbox_orphans_cleaned_total = Counter(
    "ls_sandbox_orphans_cleaned_total",
    "Number of orphaned sandbox containers cleaned up on startup",
)

ls_rate_limit_rejections_total = Counter(
    "ls_rate_limit_rejections_total",
    "Total number of requests rejected by per-caller rate limiting",
    ["path"],
)

ls_alert_triggers_total = Counter(
    "ls_alert_triggers_total",
    "Total number of Alertmanager alert trigger outcomes",
    ["workflow_name", "status"],
)

ls_schedule_triggers_total = Counter(
    "ls_schedule_triggers_total",
    "Total number of schedule trigger outcomes",
    ["workflow_name", "status"],
)

ls_sandbox_tls_errors_total = Counter(
    "ls_sandbox_tls_errors_total",
    "Total number of TLS-related errors during sandbox communication",
    ["step_name", "error_type"],
)
