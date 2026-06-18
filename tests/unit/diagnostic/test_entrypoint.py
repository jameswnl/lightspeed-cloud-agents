"""Unit tests for diagnostic agent entrypoint."""

from fastapi.testclient import TestClient

from agents.diagnostic.entrypoint import app


class TestEntrypoint:
    """Tests for the diagnostic agent entrypoint app."""

    def test_app_is_created(self) -> None:
        """Test that the entrypoint creates a valid FastAPI app."""
        assert app is not None
        assert "diagnostic-agent" in app.title

    def test_healthz_via_entrypoint(self) -> None:
        """Test that /healthz works through the entrypoint app."""
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["agent_name"] == "diagnostic-agent"

    def test_cluster_state_initialized(self) -> None:
        """Test that cluster state is initialized on import."""
        from agents.diagnostic.cluster_state import cluster_state
        assert "hosts" in cluster_state
        assert len(cluster_state["hosts"]) == 4
