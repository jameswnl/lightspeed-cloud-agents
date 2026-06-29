"""Unit tests for TokenReview auth middleware (Phase 8 Task 10)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.runtime.auth import TokenReviewAuthMiddleware, get_auth_mode


def _make_app_with_token_review() -> TestClient:
    """Create a test app with TokenReview middleware."""
    app = FastAPI()
    app.add_middleware(TokenReviewAuthMiddleware)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ready"}

    @app.get("/v1/test")
    async def test_endpoint():
        return {"ok": True}

    return TestClient(app)


class TestTokenReviewAuth:
    """Tests for TokenReviewAuthMiddleware."""

    def test_healthz_exempt(self) -> None:
        """Healthz is accessible without token."""
        tc = _make_app_with_token_review()
        resp = tc.get("/healthz")
        assert resp.status_code == 200

    def test_missing_token_returns_401(self) -> None:
        """No bearer token returns 401."""
        tc = _make_app_with_token_review()
        resp = tc.get("/v1/test")
        assert resp.status_code == 401

    def test_valid_token_accepted(self) -> None:
        """Valid token passes through."""
        import sys

        mock_k8s_client = MagicMock()
        mock_auth_api = MagicMock()
        mock_result = MagicMock()
        mock_result.status.authenticated = True
        mock_auth_api.create_token_review.return_value = mock_result
        mock_k8s_client.AuthenticationV1Api.return_value = mock_auth_api

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = MagicMock()

        tc = _make_app_with_token_review()
        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s.config,
        }):
            resp = tc.get("/v1/test", headers={"Authorization": "Bearer valid-sa-token"})

        assert resp.status_code == 200

    def test_invalid_token_rejected(self) -> None:
        """Invalid token returns 401."""
        import sys

        mock_k8s_client = MagicMock()
        mock_auth_api = MagicMock()
        mock_result = MagicMock()
        mock_result.status.authenticated = False
        mock_auth_api.create_token_review.return_value = mock_result
        mock_k8s_client.AuthenticationV1Api.return_value = mock_auth_api

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = MagicMock()

        tc = _make_app_with_token_review()
        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s.config,
        }):
            resp = tc.get("/v1/test", headers={"Authorization": "Bearer bad-token"})

        assert resp.status_code == 401


class TestGetRunnerAuthToken:
    """Tests for get_runner_auth_token()."""

    def test_shared_secret_returns_api_token(self) -> None:
        """In shared_secret mode, returns AGENT_API_TOKEN."""
        import os
        from agents.runtime.auth import get_runner_auth_token
        with patch.dict(os.environ, {"AUTH_MODE": "shared_secret", "AGENT_API_TOKEN": "shared-tok"}):
            assert get_runner_auth_token() == "shared-tok"

    def test_shared_secret_returns_none_when_empty(self) -> None:
        """In shared_secret mode with no token, returns None."""
        import os
        from agents.runtime.auth import get_runner_auth_token
        with patch.dict(os.environ, {"AUTH_MODE": "shared_secret"}, clear=True):
            assert get_runner_auth_token() is None

    def test_sa_token_reads_projected_file(self) -> None:
        """In sa_token mode, reads from projected volume path."""
        import os
        import tempfile
        from agents.runtime.auth import get_runner_auth_token, SA_TOKEN_PATH

        with tempfile.NamedTemporaryFile(mode="w", suffix=".token", delete=False) as f:
            f.write("projected-runner-token")
            tmp_path = f.name

        try:
            with patch.dict(os.environ, {"AUTH_MODE": "sa_token"}), \
                 patch("agents.runtime.auth.SA_TOKEN_PATH", tmp_path):
                from importlib import reload
                import agents.runtime.auth as auth_mod
                original_path = auth_mod.SA_TOKEN_PATH
                auth_mod.SA_TOKEN_PATH = tmp_path
                try:
                    result = get_runner_auth_token()
                    assert result == "projected-runner-token"
                finally:
                    auth_mod.SA_TOKEN_PATH = original_path
        finally:
            os.unlink(tmp_path)

    def test_sa_token_returns_none_when_file_missing(self) -> None:
        """In sa_token mode with missing file, returns None."""
        import os
        from agents.runtime.auth import get_runner_auth_token
        with patch.dict(os.environ, {"AUTH_MODE": "sa_token"}):
            assert get_runner_auth_token() is None


class TestWorkflowRunnerManifest:
    """Tests that K8s manifest provides projected SA token."""

    def test_rbac_grants_tokenreview_permission(self) -> None:
        """Verify rbac.yaml grants tokenreviews create to workflow-runner."""
        import os
        import yaml

        rbac_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "deploy", "kind", "rbac.yaml",
        )
        with open(rbac_path) as f:
            docs = list(yaml.safe_load_all(f))

        cluster_roles = [d for d in docs if d.get("kind") == "ClusterRole"]
        tokenreview_role = next(
            (r for r in cluster_roles if r["metadata"]["name"] == "workflow-runner-tokenreview"),
            None,
        )
        assert tokenreview_role is not None, "ClusterRole workflow-runner-tokenreview not found"

        rules = tokenreview_role["rules"]
        tr_rule = next(
            (r for r in rules if "tokenreviews" in r.get("resources", [])),
            None,
        )
        assert tr_rule is not None
        assert "authentication.k8s.io" in tr_rule["apiGroups"]
        assert "create" in tr_rule["verbs"]

    def test_runner_manifest_has_projected_token_volume(self) -> None:
        """Verify workflow-runner.yaml mounts projected SA token."""
        import os
        import yaml

        manifest_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "deploy", "kind", "workflow-runner.yaml",
        )
        with open(manifest_path) as f:
            docs = list(yaml.safe_load_all(f))

        deployment = next(d for d in docs if d.get("kind") == "Deployment")
        spec = deployment["spec"]["template"]["spec"]

        volume_names = [v["name"] for v in spec.get("volumes", [])]
        assert "sa-token" in volume_names

        sa_vol = next(v for v in spec["volumes"] if v["name"] == "sa-token")
        token_source = sa_vol["projected"]["sources"][0]["serviceAccountToken"]
        assert token_source["audience"] == "cloud-agents"
        assert token_source["path"] == "token"

        container = spec["containers"][0]
        mount_paths = [m["mountPath"] for m in container.get("volumeMounts", [])]
        assert "/var/run/secrets/cloud-agents" in mount_paths

        env_dict = {e["name"]: e.get("value") for e in container.get("env", [])}
        assert "TEMPORAL_URL" in env_dict


class TestGetAuthMode:
    """Tests for auth mode selection."""

    def test_default_is_shared_secret(self) -> None:
        """Default auth mode is shared_secret."""
        import os
        with patch.dict(os.environ, {}, clear=True):
            assert get_auth_mode() == "shared_secret"

    def test_sa_token_mode(self) -> None:
        """AUTH_MODE=sa_token selects token review."""
        import os
        with patch.dict(os.environ, {"AUTH_MODE": "sa_token"}):
            assert get_auth_mode() == "sa_token"
