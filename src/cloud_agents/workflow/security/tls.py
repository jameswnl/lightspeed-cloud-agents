"""Ephemeral TLS certificate generation for runner-to-sandbox encryption.

Generates a CA + server certificate pair per sandbox spawn. The runner
keeps the CA cert (for verification) and the sandbox gets the server
cert + key (for serving HTTPS). Certificates are short-lived (default
10 minutes) so compromise of a single cert has bounded impact.

Uses EC P-256 keys for fast generation (~2ms vs ~100ms for RSA).
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class TLSMode(str, Enum):
    """TLS mode for runner-to-sandbox communication.

    Attributes:
        DISABLED: No TLS (default, backward compatible).
        APP: App-level TLS with ephemeral certs.
        MESH: Service mesh handles TLS, skip app-level.
    """

    DISABLED = "disabled"
    APP = "app"
    MESH = "mesh"


def get_tls_mode() -> TLSMode:
    """Read TLS mode from SANDBOX_TLS_MODE env var.

    Returns:
        TLSMode enum value.

    Raises:
        ValueError: If the env var contains an invalid mode.
    """
    raw = os.environ.get("SANDBOX_TLS_MODE", "disabled")
    return TLSMode(raw)


@dataclass
class EphemeralCerts:
    """Bundle of ephemeral certificates for one sandbox spawn.

    Attributes:
        ca_cert_pem: PEM-encoded CA certificate (runner keeps this).
        server_cert_pem: PEM-encoded server certificate (sandbox gets this).
        server_key_pem: PEM-encoded server private key (sandbox gets this).
        valid_seconds: How long the certs are valid.
    """

    ca_cert_pem: bytes
    server_cert_pem: bytes
    server_key_pem: bytes
    valid_seconds: int


def generate_ephemeral_certs(
    common_name: str,
    san_dns: list[str] | None = None,
    san_ips: list[str] | None = None,
    valid_seconds: int = 600,
) -> EphemeralCerts:
    """Generate a CA + server certificate pair.

    Parameters:
        common_name: CN for the server cert (e.g., pod name).
        san_dns: Subject Alternative Name DNS entries.
        san_ips: Subject Alternative Name IP entries.
        valid_seconds: Certificate validity period (default 10 minutes).

    Returns:
        EphemeralCerts with CA cert, server cert, and server key.

    Raises:
        RuntimeError: If the cryptography library is not installed.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise RuntimeError(
            "SANDBOX_TLS_MODE=app requires the 'cryptography' package. "
            "Install with: pip install 'lightspeed-cloud-agents[tls]'"
        ) from None

    now = datetime.now(timezone.utc)
    not_valid_after = now + timedelta(seconds=valid_seconds)

    # Generate CA key pair (EC P-256)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"ephemeral-ca-{common_name}"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "cloud-agents"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(not_valid_after)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Generate server key pair (EC P-256)
    server_key = ec.generate_private_key(ec.SECP256R1())

    server_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    # Build SAN entries
    san_entries: list[x509.GeneralName] = []
    for dns in san_dns or []:
        san_entries.append(x509.DNSName(dns))
    for ip_str in san_ips or []:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))

    builder = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(not_valid_after)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )

    if san_entries:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )

    server_cert = builder.sign(ca_key, hashes.SHA256())

    # Serialize to PEM
    ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    server_cert_pem = server_cert.public_bytes(serialization.Encoding.PEM)
    server_key_pem = server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    logger.info(
        "Generated ephemeral certs for '%s' (valid %ds)",
        common_name,
        valid_seconds,
    )

    return EphemeralCerts(
        ca_cert_pem=ca_cert_pem,
        server_cert_pem=server_cert_pem,
        server_key_pem=server_key_pem,
        valid_seconds=valid_seconds,
    )
