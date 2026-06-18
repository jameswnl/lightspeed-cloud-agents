"""Unit tests for simulated cluster state."""

from agents.diagnostic.cluster_state import (
    action_log,
    cluster_state,
    reset_cluster_healthy,
    simulate_bad_deploy,
    simulate_disk_growth,
)


class TestResetCluster:
    """Tests for reset_cluster_healthy."""

    def test_all_hosts_healthy(self) -> None:
        """Test that reset produces four healthy hosts."""
        reset_cluster_healthy()
        for host in cluster_state["hosts"].values():
            assert host["status"] == "healthy"

    def test_four_hosts_present(self) -> None:
        """Test that reset produces exactly four hosts."""
        reset_cluster_healthy()
        assert len(cluster_state["hosts"]) == 4
        assert set(cluster_state["hosts"].keys()) == {
            "web-01", "web-02", "db-01", "cache-01"
        }

    def test_no_alerts_or_deploys(self) -> None:
        """Test that reset clears alerts and deploys."""
        reset_cluster_healthy()
        assert cluster_state["alerts"] == []
        assert cluster_state["recent_deploys"] == []

    def test_action_log_cleared(self) -> None:
        """Test that reset clears the action log."""
        action_log.append({"test": "data"})
        reset_cluster_healthy()
        assert action_log == []


class TestSimulateBadDeploy:
    """Tests for simulate_bad_deploy."""

    def test_web02_becomes_degraded(self) -> None:
        """Test that web-02 becomes degraded after bad deploy."""
        reset_cluster_healthy()
        simulate_bad_deploy()
        host = cluster_state["hosts"]["web-02"]
        assert host["status"] == "degraded"
        assert host["cpu"] == 92
        assert host["services"]["app"] == "crashed"

    def test_deploy_recorded(self) -> None:
        """Test that the deploy is recorded."""
        reset_cluster_healthy()
        simulate_bad_deploy()
        assert len(cluster_state["recent_deploys"]) == 1
        deploy = cluster_state["recent_deploys"][0]
        assert deploy["host"] == "web-02"
        assert deploy["app"] == "frontend"
        assert deploy["version"] == "v2.3.1"

    def test_alert_added(self) -> None:
        """Test that an alert is added."""
        reset_cluster_healthy()
        simulate_bad_deploy()
        assert len(cluster_state["alerts"]) == 1
        assert "web-02" in cluster_state["alerts"][0]

    def test_other_hosts_unaffected(self) -> None:
        """Test that other hosts remain healthy."""
        reset_cluster_healthy()
        simulate_bad_deploy()
        assert cluster_state["hosts"]["web-01"]["status"] == "healthy"
        assert cluster_state["hosts"]["db-01"]["status"] == "healthy"


class TestSimulateDiskGrowth:
    """Tests for simulate_disk_growth."""

    def test_disk_set_to_target(self) -> None:
        """Test that disk percentage is set."""
        reset_cluster_healthy()
        simulate_disk_growth("db-01", 82)
        assert cluster_state["hosts"]["db-01"]["disk"] == 82

    def test_warning_status_at_90(self) -> None:
        """Test that status becomes warning at 90%+."""
        reset_cluster_healthy()
        simulate_disk_growth("db-01", 92)
        assert cluster_state["hosts"]["db-01"]["status"] == "warning"

    def test_no_warning_below_90(self) -> None:
        """Test that status stays healthy below 90%."""
        reset_cluster_healthy()
        simulate_disk_growth("db-01", 82)
        assert cluster_state["hosts"]["db-01"]["status"] == "healthy"

    def test_alert_added_at_90(self) -> None:
        """Test that alert is added at 90%+."""
        reset_cluster_healthy()
        simulate_disk_growth("db-01", 92)
        assert any("db-01" in a for a in cluster_state["alerts"])
