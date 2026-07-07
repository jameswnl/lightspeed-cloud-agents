"""Unit tests for load test helper utilities (TDD)."""

from __future__ import annotations

import pytest


class TestLatencyTracker:
    """Tests for the LatencyTracker utility."""

    def test_record_and_percentile_p50(self) -> None:
        """p50 of [1, 2, 3, 4, 5] should be 3."""
        from tests.load.helpers import LatencyTracker

        tracker = LatencyTracker()
        for val in [1.0, 2.0, 3.0, 4.0, 5.0]:
            tracker.record(val)
        assert tracker.percentile(50) == pytest.approx(3.0)

    def test_record_and_percentile_p99(self) -> None:
        """p99 of 100 sequential values should be near 100."""
        from tests.load.helpers import LatencyTracker

        tracker = LatencyTracker()
        for val in range(1, 101):
            tracker.record(float(val))
        assert tracker.percentile(99) >= 99.0

    def test_percentile_empty_raises(self) -> None:
        """Requesting percentile on empty tracker raises ValueError."""
        from tests.load.helpers import LatencyTracker

        tracker = LatencyTracker()
        with pytest.raises(ValueError, match="No latencies recorded"):
            tracker.percentile(50)

    def test_summary_returns_dict(self) -> None:
        """summary() returns dict with p50, p95, p99, count, total keys."""
        from tests.load.helpers import LatencyTracker

        tracker = LatencyTracker()
        for val in [0.1, 0.2, 0.3]:
            tracker.record(val)
        result = tracker.summary()
        assert set(result.keys()) == {"p50", "p95", "p99", "count", "total"}
        assert result["count"] == 3

    def test_count_tracks_recordings(self) -> None:
        """count property returns number of recorded values."""
        from tests.load.helpers import LatencyTracker

        tracker = LatencyTracker()
        assert tracker.count == 0
        tracker.record(1.0)
        tracker.record(2.0)
        assert tracker.count == 2


class TestWorkflowFactory:
    """Tests for the WorkflowFactory helper."""

    def test_creates_valid_run_request(self) -> None:
        """Factory produces a dict with required fields for /v1/workflows/run."""
        from tests.load.helpers import WorkflowFactory

        factory = WorkflowFactory()
        payload = factory.run_request()
        assert "definition" in payload
        assert "provider" in payload
        assert payload["definition"]["apiVersion"] == "v1"
        assert payload["definition"]["kind"] == "AgentWorkflow"

    def test_unique_workflow_ids(self) -> None:
        """Each call produces a unique workflow_id."""
        from tests.load.helpers import WorkflowFactory

        factory = WorkflowFactory()
        ids = {factory.run_request()["workflow_id"] for _ in range(50)}
        assert len(ids) == 50

    def test_custom_prefix(self) -> None:
        """workflow_id uses the provided prefix."""
        from tests.load.helpers import WorkflowFactory

        factory = WorkflowFactory(id_prefix="load-test")
        payload = factory.run_request()
        assert payload["workflow_id"].startswith("load-test-")

    def test_with_approval_step(self) -> None:
        """run_request_with_approval includes an approval step."""
        from tests.load.helpers import WorkflowFactory

        factory = WorkflowFactory()
        payload = factory.run_request_with_approval()
        steps = payload["definition"]["spec"]["steps"]
        approval_steps = [s for s in steps if s.get("approval_required")]
        assert len(approval_steps) >= 1


class TestResponseCollector:
    """Tests for the ResponseCollector helper."""

    def test_add_and_counts(self) -> None:
        """Tracks success and error counts correctly."""
        from tests.load.helpers import ResponseCollector

        collector = ResponseCollector()
        collector.add(status_code=202, latency=0.1)
        collector.add(status_code=202, latency=0.2)
        collector.add(status_code=429, latency=0.05)
        assert collector.success_count == 2
        assert collector.error_count == 1
        assert collector.total_count == 3

    def test_status_code_distribution(self) -> None:
        """status_codes property returns counts per status code."""
        from tests.load.helpers import ResponseCollector

        collector = ResponseCollector()
        collector.add(status_code=202, latency=0.1)
        collector.add(status_code=202, latency=0.2)
        collector.add(status_code=429, latency=0.05)
        collector.add(status_code=500, latency=0.3)
        assert collector.status_codes[202] == 2
        assert collector.status_codes[429] == 1
        assert collector.status_codes[500] == 1

    def test_success_rate(self) -> None:
        """success_rate returns fraction of 2xx responses."""
        from tests.load.helpers import ResponseCollector

        collector = ResponseCollector()
        collector.add(status_code=202, latency=0.1)
        collector.add(status_code=200, latency=0.1)
        collector.add(status_code=429, latency=0.05)
        collector.add(status_code=500, latency=0.3)
        assert collector.success_rate == pytest.approx(0.5)

    def test_empty_success_rate_zero(self) -> None:
        """success_rate is 0.0 when no responses recorded."""
        from tests.load.helpers import ResponseCollector

        collector = ResponseCollector()
        assert collector.success_rate == 0.0
