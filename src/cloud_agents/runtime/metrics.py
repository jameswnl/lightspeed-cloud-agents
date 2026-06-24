"""Prometheus metrics for agent runtime.

Follows the existing ls_* naming convention from src/metrics/.
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

ls_agent_tool_calls_total = Counter(
    "ls_agent_tool_calls_total",
    "Total number of tool calls by agent",
    ["agent_name", "tool_name", "status"],
)

ls_agent_tool_duration_seconds = Histogram(
    "ls_agent_tool_duration_seconds",
    "Duration of tool calls in seconds",
    ["agent_name", "tool_name"],
    buckets=(0.1, 0.5, 1, 5, 10, 30, 60),
)
