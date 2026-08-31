"""Unit tests for cloud_agents.spawner.factory.build_spawner (issue #182)."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture


class TestBuildSpawnerOpenShell:
    """Tests for the openshell branch, including TLS/bearer-token wiring."""

    def test_builds_openshell_spawner_no_auth(self, mocker: MockerFixture) -> None:
        """No TLS/bearer params -> plain SandboxClient(endpoint=...)."""
        from cloud_agents.spawner.factory import build_spawner
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

        mock_client = mocker.patch("openshell.SandboxClient")

        spawner = build_spawner("openshell", gateway_url="gw:17670")

        mock_client.assert_called_once_with(endpoint="gw:17670")
        assert isinstance(spawner, OpenShellSpawner)
        assert spawner._endpoint == "gw:17670"
        assert spawner._tls_ca == ""
        assert spawner._bearer_token == ""

    def test_strips_http_scheme_from_gateway_url(self, mocker: MockerFixture) -> None:
        """http(s):// prefix is stripped before building the gRPC endpoint."""
        from cloud_agents.spawner.factory import build_spawner

        mock_client = mocker.patch("openshell.SandboxClient")

        spawner = build_spawner("openshell", gateway_url="https://gw.example.com:443")

        mock_client.assert_called_once_with(endpoint="gw.example.com:443")
        assert spawner._endpoint == "gw.example.com:443"

    def test_mtls_wiring(self, mocker: MockerFixture, tmp_path: Any) -> None:
        """tls_ca + tls_cert + tls_key builds a full TlsConfig and passes it through."""
        from cloud_agents.spawner.factory import build_spawner

        ca = tmp_path / "ca.pem"
        ca.write_text("ca")
        cert = tmp_path / "client.pem"
        cert.write_text("cert")
        key = tmp_path / "client.key"
        key.write_text("key")

        mock_client = mocker.patch("openshell.SandboxClient")
        mock_tls_config = mocker.patch("openshell.TlsConfig")

        spawner = build_spawner(
            "openshell",
            gateway_url="gw:17670",
            tls_ca=str(ca),
            tls_cert=str(cert),
            tls_key=str(key),
        )

        mock_tls_config.assert_called_with(ca_path=ca, cert_path=cert, key_path=key)
        assert "tls" in mock_client.call_args.kwargs
        assert spawner._tls_ca == str(ca)

    def test_ca_only_tls_wiring(self, mocker: MockerFixture, tmp_path: Any) -> None:
        """tls_ca alone (no cert/key) still builds a CA-only TlsConfig."""
        from cloud_agents.spawner.factory import build_spawner

        ca = tmp_path / "ca.pem"
        ca.write_text("ca")

        mock_client = mocker.patch("openshell.SandboxClient")
        mock_tls_config = mocker.patch("openshell.TlsConfig")

        build_spawner("openshell", gateway_url="gw:17670", tls_ca=str(ca))

        mock_tls_config.assert_called_once_with(ca_path=ca)
        assert "tls" in mock_client.call_args.kwargs

    def test_bearer_token_wiring(self, mocker: MockerFixture) -> None:
        """bearer_token is passed to SandboxClient and stored on the spawner."""
        from cloud_agents.spawner.factory import build_spawner

        mock_client = mocker.patch("openshell.SandboxClient")

        spawner = build_spawner("openshell", gateway_url="gw:17670", bearer_token="tok-123")

        assert mock_client.call_args.kwargs["bearer_token"] == "tok-123"
        assert spawner._bearer_token == "tok-123"

    def test_http_endpoint_override_passthrough(self, mocker: MockerFixture) -> None:
        """http_endpoint is forwarded to OpenShellSpawner unchanged."""
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")

        spawner = build_spawner(
            "openshell", gateway_url="gw:17670", http_endpoint="https://proxy.example.com"
        )

        assert spawner._http_endpoint == "https://proxy.example.com"

    def test_none_params_fall_back_to_defaults(self, mocker: MockerFixture) -> None:
        """Explicit None (e.g. from an unset Pydantic Optional field) behaves like omitted."""
        from cloud_agents.spawner.factory import build_spawner

        mock_client = mocker.patch("openshell.SandboxClient")

        spawner = build_spawner(
            "openshell",
            gateway_url=None,
            workspace=None,
            tls_ca=None,
            tls_cert=None,
            tls_key=None,
            bearer_token=None,
        )

        mock_client.assert_called_once_with(endpoint="localhost:17670")
        assert spawner._workspace == "default"
        assert spawner._tls_ca == ""
        assert spawner._bearer_token == ""

    def test_default_gateway_url_when_omitted(self, mocker: MockerFixture) -> None:
        """Omitting gateway_url entirely uses the same default as the old env-based path."""
        from cloud_agents.spawner.factory import build_spawner

        mock_client = mocker.patch("openshell.SandboxClient")

        build_spawner("openshell")

        mock_client.assert_called_once_with(endpoint="localhost:17670")

    def test_unknown_extra_keys_are_dropped_not_forwarded(self, mocker: MockerFixture) -> None:
        """Extra unrelated keys (e.g. from a broader config dict) don't TypeError.

        OpenShellSpawner forwards its own unrecognized kwargs to
        AgentSpawner.__init__(max_pods=...), same as kubernetes/podman.
        """
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")

        spawner = build_spawner(
            "openshell",
            gateway_url="gw:17670",
            type="openshell",
            sandbox_image="sandbox:latest",
        )

        assert spawner._endpoint == "gw:17670"

    def test_extra_readable_paths_forwarded(self, mocker: MockerFixture) -> None:
        """extra_readable_paths reaches OpenShellSpawner (issue #189).

        _OPENSHELL_EXTRA_PARAMS previously only allowed `max_pods` through
        -- an unlisted kwarg like extra_readable_paths would be silently
        dropped by build_spawner's kwarg filtering, even though
        OpenShellSpawner's own constructor accepts it.
        """
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")

        spawner = build_spawner(
            "openshell",
            gateway_url="gw:17670",
            extra_readable_paths=["/opt/custom", "/srv/app"],
        )

        assert spawner._extra_readable_paths == ["/opt/custom", "/srv/app"]

    def test_extra_readable_paths_none_falls_back_to_spawner_default(
        self, mocker: MockerFixture
    ) -> None:
        """Omitting extra_readable_paths (or passing None) uses OpenShellSpawner's own default.

        None must be dropped rather than forwarded, same as other Optional
        fields -- forwarding None would override the spawner's own
        ["/opt/app-root", "/opt/lightspeed"] default with an empty value.
        """
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")

        spawner = build_spawner("openshell", gateway_url="gw:17670", extra_readable_paths=None)

        assert spawner._extra_readable_paths == ["/opt/app-root", "/opt/lightspeed"]

    def test_extra_env_forwarded(self, mocker: MockerFixture) -> None:
        """extra_env reaches OpenShellSpawner (issue #192).

        Same gotcha as extra_readable_paths (#189): an unlisted kwarg
        would be silently dropped by build_spawner's kwarg filtering, even
        though OpenShellSpawner's own constructor accepts it.
        """
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")

        spawner = build_spawner(
            "openshell",
            gateway_url="gw:17670",
            extra_env={"PYTHONPATH": "/custom/path"},
        )

        assert spawner._extra_env == {"PYTHONPATH": "/custom/path"}

    def test_extra_env_none_falls_back_to_spawner_default(self, mocker: MockerFixture) -> None:
        """Omitting extra_env (or passing None) uses OpenShellSpawner's own default."""
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")

        spawner = build_spawner("openshell", gateway_url="gw:17670", extra_env=None)

        assert spawner._extra_env == {
            "PYTHONPATH": "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages",
            "LIGHTSPEED_SKILLS_DIR": "/app/skills",
        }


class TestBuildSpawnerOpenShellBearerTokenProvider:
    """Tests for bearer_token_provider wiring (issue #236)."""

    def test_provider_forwarded_to_sandbox_client_and_spawner(
        self, mocker: MockerFixture
    ) -> None:
        """bearer_token_provider is forwarded as-is to both SandboxClient
        (bearer_token=) and OpenShellSpawner (bearer_token_provider=)."""
        from cloud_agents.spawner.factory import build_spawner

        mock_client = mocker.patch("openshell.SandboxClient")
        provider = mocker.Mock(return_value="fresh-token")

        spawner = build_spawner(
            "openshell",
            gateway_url="gw:17670",
            bearer_token_provider=provider,
        )

        assert mock_client.call_args.kwargs["bearer_token"] is provider
        assert spawner._bearer_token_provider is provider
        assert spawner._bearer_token == ""

    def test_static_bearer_token_takes_precedence_over_provider(
        self, mocker: MockerFixture
    ) -> None:
        """If both are (mistakenly) passed, the factory prefers the static
        token for SandboxClient wiring -- but OpenShellSpawner's own
        constructor still raises ValueError for the ambiguous config."""
        from cloud_agents.spawner.factory import build_spawner

        mocker.patch("openshell.SandboxClient")
        provider = mocker.Mock(return_value="fresh-token")

        with pytest.raises(ValueError, match="mutually exclusive"):
            build_spawner(
                "openshell",
                gateway_url="gw:17670",
                bearer_token="static-token",
                bearer_token_provider=provider,
            )

    def test_no_provider_no_bearer_token_kwarg(self, mocker: MockerFixture) -> None:
        """Without bearer_token/bearer_token_provider, SandboxClient gets no
        bearer_token kwarg at all."""
        from cloud_agents.spawner.factory import build_spawner

        mock_client = mocker.patch("openshell.SandboxClient")

        spawner = build_spawner("openshell", gateway_url="gw:17670")

        assert "bearer_token" not in mock_client.call_args.kwargs
        assert spawner._bearer_token_provider is None


class TestBuildSpawnerUnknownType:
    """Tests for the error path on unrecognized spawner types."""

    def test_raises_on_unknown_type(self) -> None:
        """An unrecognized spawner_type raises ValueError rather than returning None."""
        from cloud_agents.spawner.factory import build_spawner

        with pytest.raises(ValueError, match="Unknown spawner_type"):
            build_spawner("not-a-real-type")

    def test_kubernetes_and_podman_are_no_longer_valid_types(self) -> None:
        """kubernetes/podman were removed as spawner types (issue #198) --
        OpenShellSpawner is the sole ephemeral spawner; Kubernetes/Podman
        deployment targets are reached through OpenShell's own gateway
        compute driver instead of separate AgentSpawner implementations.
        """
        from cloud_agents.spawner.factory import build_spawner

        with pytest.raises(ValueError, match=r"expected one of \('openshell',\)"):
            build_spawner("kubernetes")
        with pytest.raises(ValueError, match=r"expected one of \('openshell',\)"):
            build_spawner("podman")

    def test_raises_on_empty_string(self) -> None:
        """Empty string is also treated as invalid, not as 'no spawner'.

        Callers that want "no spawner configured" as a valid state (e.g. the
        Temporal entrypoint reading an unset env var) must short-circuit
        before calling build_spawner(), not rely on it accepting "".
        """
        from cloud_agents.spawner.factory import build_spawner

        with pytest.raises(ValueError, match="Unknown spawner_type"):
            build_spawner("")
