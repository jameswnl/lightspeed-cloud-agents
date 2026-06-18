"""Prometheus metrics for agent runtime.

Follows the existing ls_* naming convention from src/metrics/.
Per-run metrics only in Phase 1b. Per-tool metrics deferred to Phase 2.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

ls_agent_runs_total = Counter(
    "ls_agent_runs_total",
    "Total number of agent runs",
    ["agent_name", "status"],
)

ls_agent_run_duration_seconds = Histogram(
    "ls_agent_run_duration_seconds",
    "Duration of agent runs in seconds",
    ["agent_name"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)
