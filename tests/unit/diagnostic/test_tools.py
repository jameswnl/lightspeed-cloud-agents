"""Unit tests for diagnostic agent tools."""

import pytest

from agents.diagnostic.cluster_state import (
    action_log,
    cluster_state,
    reset_cluster_healthy,
    simulate_bad_deploy,
)
from agents.diagnostic.tools import (
    check_host,
    get_alerts,
    get_recent_deploys,
    list_hosts,
    run_remediation,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state before each test."""
    reset_cluster_healthy()


class TestListHosts:
    """Tests for list_hosts tool."""

    def test_returns_all_hosts(self) -> None:
        """Test that all four hosts are returned."""
        hosts = list_hosts()
        assert len(hosts) == 4
        hostnames = {h["hostname"] for h in hosts}
        assert hostnames == {"web-01", "web-02", "db-01", "cache-01"}

    def test_includes_role_and_status(self) -> None:
        """Test that each host has role and status."""
        hosts = list_hosts()
        for host in hosts:
            assert "role" in host
            assert "status" in host
            assert "hostname" in host


class TestCheckHost:
    """Tests for check_host tool."""

    def test_known_host(self) -> None:
        """Test checking a known host returns full details."""
        result = check_host("web-01")
        assert result["hostname"] == "web-01"
        assert result["role"] == "webserver"
        assert "cpu" in result
        assert "memory" in result
        assert "disk" in result
        assert "services" in result

    def test_unknown_host(self) -> None:
        """Test checking an unknown host returns error."""
        result = check_host("nonexistent")
        assert "error" in result
        assert "nonexistent" in result["error"]


class TestGetAlerts:
    """Tests for get_alerts tool."""

    def test_no_alerts_initially(self) -> None:
        """Test that healthy cluster has no alerts."""
        assert get_alerts() == []

    def test_alerts_after_bad_deploy(self) -> None:
        """Test that alerts appear after bad deploy."""
        simulate_bad_deploy()
        alerts = get_alerts()
        assert len(alerts) == 1
        assert "web-02" in alerts[0]


class TestGetRecentDeploys:
    """Tests for get_recent_deploys tool."""

    def test_no_deploys_initially(self) -> None:
        """Test that healthy cluster has no deploys."""
        assert get_recent_deploys() == []

    def test_deploys_after_bad_deploy(self) -> None:
        """Test that deploys are recorded."""
        simulate_bad_deploy()
        deploys = get_recent_deploys()
        assert len(deploys) == 1
        assert deploys[0]["host"] == "web-02"


class TestRunRemediation:
    """Tests for run_remediation tool."""

    def test_restart_service(self) -> None:
        """Test restarting a service."""
        simulate_bad_deploy()
        result = run_remediation("web-02", "restart_service:app", "app crashed")
        assert result["success"] is True
        assert cluster_state["hosts"]["web-02"]["services"]["app"] == "running"
        assert cluster_state["hosts"]["web-02"]["status"] == "healthy"

    def test_rollback_deploy(self) -> None:
        """Test rolling back a deploy."""
        simulate_bad_deploy()
        result = run_remediation(
            "web-02", "rollback_deploy:frontend", "bad deploy"
        )
        assert result["success"] is True
        assert cluster_state["hosts"]["web-02"]["status"] == "healthy"

    def test_cleanup_disk(self) -> None:
        """Test cleaning disk."""
        cluster_state["hosts"]["db-01"]["disk"] = 92
        result = run_remediation("db-01", "cleanup_disk", "disk full")
        assert result["success"] is True
        assert cluster_state["hosts"]["db-01"]["disk"] < 92

    def test_cleanup_disk_already_ok(self) -> None:
        """Test cleanup when disk is already acceptable."""
        result = run_remediation("cache-01", "cleanup_disk", "preventive")
        assert result["success"] is False
        assert "acceptable" in result["error"]

    def test_unknown_host(self) -> None:
        """Test remediation on unknown host."""
        result = run_remediation("ghost", "cleanup_disk", "reason")
        assert result["success"] is False
        assert "Unknown host" in result["error"]

    def test_unknown_action(self) -> None:
        """Test unknown remediation action."""
        result = run_remediation("web-01", "do_magic", "reason")
        assert result["success"] is False
        assert "Unknown action" in result["error"]

    def test_action_logged(self) -> None:
        """Test that remediation action is logged."""
        simulate_bad_deploy()
        run_remediation("web-02", "restart_service:app", "crashed")
        assert len(action_log) == 1
        assert action_log[0]["host"] == "web-02"
        assert action_log[0]["action"] == "restart_service:app"

    def test_rollback_nonexistent_deploy(self) -> None:
        """Test rolling back a deploy that doesn't exist."""
        result = run_remediation(
            "web-01", "rollback_deploy:backend", "no deploy"
        )
        assert result["success"] is False
        assert "No recent deploy" in result["error"]

    def test_restart_nonexistent_service(self) -> None:
        """Test restarting a service that doesn't exist."""
        result = run_remediation("web-01", "restart_service:mysql", "reason")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_scale_resources(self) -> None:
        """Test scaling resources."""
        result = run_remediation("web-01", "scale_resources", "high load")
        assert result["success"] is True
        assert cluster_state["hosts"]["web-01"]["cpu"] < 45
