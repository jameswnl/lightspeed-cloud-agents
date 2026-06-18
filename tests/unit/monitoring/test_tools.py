"""Unit tests for monitoring agent tools."""

import pytest

from agents.diagnostic.cluster_state import (
    cluster_state,
    init_scenario,
)
from agents.monitoring.tools import get_cluster_summary


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state before each test."""
    init_scenario("healthy")


class TestGetClusterSummary:
    """Tests for get_cluster_summary tool."""

    def test_returns_all_hosts(self) -> None:
        """Test that all four hosts are returned."""
        summary = get_cluster_summary()
        assert len(summary) == 4
        hostnames = {h["hostname"] for h in summary}
        assert hostnames == {"web-01", "web-02", "db-01", "cache-01"}

    def test_includes_metrics(self) -> None:
        """Test that each host has cpu, memory, disk, services."""
        summary = get_cluster_summary()
        for host in summary:
            assert "cpu" in host
            assert "memory" in host
            assert "disk" in host
            assert "services" in host

    def test_reflects_degraded_state(self) -> None:
        """Test that degraded state is reflected in summary."""
        init_scenario("bad_deploy")
        summary = get_cluster_summary()
        web02 = next(h for h in summary if h["hostname"] == "web-02")
        assert web02["status"] == "degraded"
        assert web02["cpu"] == 92
        assert web02["services"]["app"] == "crashed"
