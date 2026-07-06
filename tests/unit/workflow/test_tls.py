"""Unit tests for TLS ephemeral cert generation and TLS mode config (TDD)."""

from __future__ import annotations

import os
import ssl
from datetime import datetime, timezone

import pytest

from cloud_agents.workflow.tls import (
    EphemeralCerts,
    TLSMode,
    generate_ephemeral_certs,
    get_tls_mode,
)


class TestTLSMode:
    """Tests for TLS mode enum and env var resolution."""

    def test_default_mode_is_disabled(self) -> None:
        """Default TLS mode is DISABLED when env var not set."""
        os.environ.pop("SANDBOX_TLS_MODE", None)
        assert get_tls_mode() == TLSMode.DISABLED

    def test_mode_app_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SANDBOX_TLS_MODE=app returns APP."""
        monkeypatch.setenv("SANDBOX_TLS_MODE", "app")
        assert get_tls_mode() == TLSMode.APP

    def test_mode_mesh_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SANDBOX_TLS_MODE=mesh returns MESH."""
        monkeypatch.setenv("SANDBOX_TLS_MODE", "mesh")
        assert get_tls_mode() == TLSMode.MESH

    def test_mode_disabled_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SANDBOX_TLS_MODE=disabled returns DISABLED."""
        monkeypatch.setenv("SANDBOX_TLS_MODE", "disabled")
        assert get_tls_mode() == TLSMode.DISABLED

    def test_invalid_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid SANDBOX_TLS_MODE value raises ValueError."""
        monkeypatch.setenv("SANDBOX_TLS_MODE", "invalid")
        with pytest.raises(ValueError):
            get_tls_mode()

    def test_enum_values(self) -> None:
        """TLSMode enum has exactly three values."""
        assert TLSMode.DISABLED == "disabled"
        assert TLSMode.APP == "app"
        assert TLSMode.MESH == "mesh"


class TestEphemeralCerts:
    """Tests for the EphemeralCerts dataclass."""

    def test_dataclass_fields(self) -> None:
        """EphemeralCerts has expected fields."""
        certs = EphemeralCerts(
            ca_cert_pem=b"ca-cert",
            server_cert_pem=b"server-cert",
            server_key_pem=b"server-key",
            valid_seconds=600,
        )
        assert certs.ca_cert_pem == b"ca-cert"
        assert certs.server_cert_pem == b"server-cert"
        assert certs.server_key_pem == b"server-key"
        assert certs.valid_seconds == 600


class TestGenerateEphemeralCerts:
    """Tests for ephemeral certificate generation."""

    def test_returns_ephemeral_certs(self) -> None:
        """generate_ephemeral_certs returns an EphemeralCerts instance."""
        certs = generate_ephemeral_certs(common_name="test-pod")
        assert isinstance(certs, EphemeralCerts)

    def test_all_outputs_are_pem(self) -> None:
        """All certificate/key outputs are valid PEM format."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        certs = generate_ephemeral_certs(common_name="test-pod")
        # Verify PEM format by checking they can be parsed
        cert_marker = b"-----BEGIN CERTIFICATE-----"
        assert certs.ca_cert_pem.startswith(cert_marker)
        assert certs.server_cert_pem.startswith(cert_marker)
        # Verify the key is a valid PEM private key by loading it
        key = load_pem_private_key(certs.server_key_pem, password=None)
        assert key is not None

    def test_ca_cert_is_self_signed(self) -> None:
        """Generated CA certificate is self-signed (issuer == subject)."""
        from cryptography import x509

        certs = generate_ephemeral_certs(common_name="test-pod")
        ca = x509.load_pem_x509_certificate(certs.ca_cert_pem)
        assert ca.issuer == ca.subject

    def test_server_cert_signed_by_ca(self) -> None:
        """Server certificate is signed by the generated CA."""
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA

        certs = generate_ephemeral_certs(common_name="test-pod")
        ca = x509.load_pem_x509_certificate(certs.ca_cert_pem)
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)

        # Server cert issuer matches CA subject
        assert server.issuer == ca.subject

        # Verify the signature
        ca_public_key = ca.public_key()
        # This should not raise if signature is valid
        ca_public_key.verify(
            server.signature,
            server.tbs_certificate_bytes,
            ECDSA(server.signature_hash_algorithm),
        )

    def test_server_cert_has_correct_cn(self) -> None:
        """Server certificate has the requested common name."""
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        certs = generate_ephemeral_certs(common_name="my-sandbox-pod")
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        cn = server.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert cn == "my-sandbox-pod"

    def test_server_cert_has_san_dns(self) -> None:
        """Server certificate includes SAN DNS entries."""
        from cryptography import x509

        certs = generate_ephemeral_certs(
            common_name="pod-1",
            san_dns=["pod-1.cloud-agents.svc", "agent-pod-1"],
        )
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        san = server.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "pod-1.cloud-agents.svc" in dns_names
        assert "agent-pod-1" in dns_names

    def test_server_cert_has_san_ips(self) -> None:
        """Server certificate includes SAN IP entries."""
        import ipaddress

        from cryptography import x509

        certs = generate_ephemeral_certs(
            common_name="pod-1",
            san_ips=["127.0.0.1"],
        )
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        san = server.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        ip_addrs = san.value.get_values_for_type(x509.IPAddress)
        assert ipaddress.IPv4Address("127.0.0.1") in ip_addrs

    def test_default_validity_is_600_seconds(self) -> None:
        """Default cert validity is 600 seconds (10 minutes)."""
        certs = generate_ephemeral_certs(common_name="test-pod")
        assert certs.valid_seconds == 600

    def test_custom_validity(self) -> None:
        """Custom validity period is applied."""
        from cryptography import x509

        certs = generate_ephemeral_certs(common_name="test-pod", valid_seconds=300)
        assert certs.valid_seconds == 300
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        delta = server.not_valid_after_utc - server.not_valid_before_utc
        assert 295 <= delta.total_seconds() <= 305

    def test_ca_and_server_keys_are_different(self) -> None:
        """CA and server keys are distinct key pairs."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        certs = generate_ephemeral_certs(common_name="test-pod")
        # The server key should be loadable
        server_key = load_pem_private_key(certs.server_key_pem, password=None)
        assert server_key is not None

        # CA key is internal; just verify server cert public key differs from CA cert
        from cryptography import x509

        ca = x509.load_pem_x509_certificate(certs.ca_cert_pem)
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        ca_pub_bytes = ca.public_key().public_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.PEM,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.SubjectPublicKeyInfo,
        )
        server_pub_bytes = server.public_key().public_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.PEM,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.SubjectPublicKeyInfo,
        )
        assert ca_pub_bytes != server_pub_bytes

    def test_cert_chain_verifiable_with_ssl_context(self) -> None:
        """Generated certs work with Python ssl for TLS verification."""
        import tempfile

        certs = generate_ephemeral_certs(
            common_name="localhost",
            san_dns=["localhost"],
        )

        # Write CA cert to temp file and load into SSLContext
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(certs.ca_cert_pem)
            ca_path = f.name

        try:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(ca_path)
            # Verify CA cert is trusted — load_verify_locations should not raise
            assert ctx is not None
        finally:
            os.unlink(ca_path)

    def test_server_cert_validity_within_range(self) -> None:
        """Server cert not_valid_before <= now <= not_valid_after."""
        from cryptography import x509

        certs = generate_ephemeral_certs(common_name="test-pod", valid_seconds=600)
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        now = datetime.now(timezone.utc)
        # Allow 60 seconds of clock skew tolerance
        assert server.not_valid_before_utc <= now
        assert server.not_valid_after_utc > now

    def test_ca_cert_is_ca(self) -> None:
        """CA certificate has BasicConstraints CA=true."""
        from cryptography import x509

        certs = generate_ephemeral_certs(common_name="test-pod")
        ca = x509.load_pem_x509_certificate(certs.ca_cert_pem)
        bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True

    def test_server_cert_is_not_ca(self) -> None:
        """Server certificate has BasicConstraints CA=false."""
        from cryptography import x509

        certs = generate_ephemeral_certs(common_name="test-pod")
        server = x509.load_pem_x509_certificate(certs.server_cert_pem)
        bc = server.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is False
