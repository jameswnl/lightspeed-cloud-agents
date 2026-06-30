"""Unit tests for agent runtime Prometheus metrics."""

from prometheus_client import REGISTRY

from cloud_agents.runtime.metrics import ls_agent_run_duration_seconds, ls_agent_runs_total


class TestAgentMetrics:
    """Tests for ls_agent_* Prometheus metrics."""

    def test_counter_increments(self) -> None:
        """Test that the runs counter increments."""
        before = ls_agent_runs_total.labels(
            agent_name="test", status="success"
        )._value.get()
        ls_agent_runs_total.labels(agent_name="test", status="success").inc()
        after = ls_agent_runs_total.labels(
            agent_name="test", status="success"
        )._value.get()
        assert after == before + 1

    def test_histogram_records_duration(self) -> None:
        """Test that the duration histogram records observations."""
        ls_agent_run_duration_seconds.labels(agent_name="test").observe(5.0)
        sample = REGISTRY.get_sample_value(
            "ls_agent_run_duration_seconds_count",
            {"agent_name": "test"},
        )
        assert sample is not None
        assert sample >= 1
