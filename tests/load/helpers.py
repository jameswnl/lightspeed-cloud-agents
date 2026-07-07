"""Shared helper utilities for load and stress tests.

Provides latency tracking, workflow payload generation, and response
collection utilities used across all load test scenarios.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field


class LatencyTracker:
    """Records request latencies and computes percentile statistics.

    Stores individual latency samples and provides percentile
    calculations for p50/p95/p99 reporting.

    Attributes:
        _latencies: List of recorded latency values in seconds.
    """

    def __init__(self) -> None:
        """Initialize an empty latency tracker."""
        self._latencies: list[float] = []

    def record(self, latency: float) -> None:
        """Record a latency measurement.

        Parameters:
            latency: Latency value in seconds.
        """
        self._latencies.append(latency)

    @property
    def count(self) -> int:
        """Return the number of recorded latency values."""
        return len(self._latencies)

    def percentile(self, pct: float) -> float:
        """Compute the given percentile of recorded latencies.

        Parameters:
            pct: Percentile to compute (0-100).

        Returns:
            The computed percentile value.

        Raises:
            ValueError: If no latencies have been recorded.
        """
        if not self._latencies:
            raise ValueError("No latencies recorded")
        sorted_vals = sorted(self._latencies)
        idx = int(len(sorted_vals) * pct / 100)
        idx = min(idx, len(sorted_vals) - 1)
        return sorted_vals[idx]

    def summary(self) -> dict[str, float]:
        """Return a summary dict with p50, p95, p99, count, and total.

        Returns:
            Dictionary with percentile values, count, and total sum.
        """
        return {
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
            "count": float(self.count),
            "total": sum(self._latencies),
        }


class WorkflowFactory:
    """Generates valid workflow run request payloads for load testing.

    Produces unique workflow IDs and consistent request structures
    that match the RunWorkflowRequest schema.

    Attributes:
        _prefix: Prefix string for generated workflow IDs.
    """

    def __init__(self, id_prefix: str = "load") -> None:
        """Initialize the factory.

        Parameters:
            id_prefix: Prefix for generated workflow_id values.
        """
        self._prefix = id_prefix

    def _make_id(self) -> str:
        """Generate a unique workflow ID with the configured prefix.

        Returns:
            Unique workflow ID string.
        """
        return f"{self._prefix}-{uuid.uuid4().hex[:12]}"

    def run_request(self) -> dict:
        """Generate a valid workflow run request payload.

        Returns:
            Dict matching the RunWorkflowRequest schema with a single
            agent step, provider config, and unique workflow_id.
        """
        return {
            "workflow_id": self._make_id(),
            "definition": {
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "load-test-workflow"},
                "spec": {
                    "steps": [
                        {
                            "name": "analyze",
                            "type": "agent",
                            "output_key": "result",
                            "prompt": "Analyze the system status.",
                        }
                    ]
                },
            },
            "provider": {
                "name": "openai",
                "model": "gpt-4",
                "credentials_secret": "openai-api-key",
            },
        }

    def run_request_with_approval(self) -> dict:
        """Generate a workflow request with an approval-gated step.

        Returns:
            Dict matching the RunWorkflowRequest schema with two steps:
            one regular agent step and one requiring approval.
        """
        payload = self.run_request()
        payload["definition"]["spec"]["steps"] = [
            {
                "name": "analyze",
                "type": "agent",
                "output_key": "analysis",
                "prompt": "Analyze the system.",
            },
            {
                "name": "remediate",
                "type": "agent",
                "output_key": "fix",
                "prompt": "Apply the fix.",
                "approval_required": True,
            },
        ]
        return payload


@dataclass
class ResponseCollector:
    """Collects HTTP response results from load test runs.

    Tracks status codes, latencies, and computes aggregate statistics
    for success rates and error distributions.

    Attributes:
        _results: List of (status_code, latency) tuples.
    """

    _results: list[tuple[int, float]] = field(default_factory=list)

    def add(self, status_code: int, latency: float) -> None:
        """Record a response result.

        Parameters:
            status_code: HTTP status code of the response.
            latency: Request latency in seconds.
        """
        self._results.append((status_code, latency))

    @property
    def total_count(self) -> int:
        """Return total number of recorded responses."""
        return len(self._results)

    @property
    def success_count(self) -> int:
        """Return count of 2xx responses."""
        return sum(1 for code, _ in self._results if 200 <= code < 300)

    @property
    def error_count(self) -> int:
        """Return count of non-2xx responses."""
        return sum(1 for code, _ in self._results if code < 200 or code >= 300)

    @property
    def status_codes(self) -> Counter:
        """Return a Counter of status codes."""
        return Counter(code for code, _ in self._results)

    @property
    def success_rate(self) -> float:
        """Return fraction of 2xx responses (0.0 if no responses)."""
        if not self._results:
            return 0.0
        return self.success_count / self.total_count
