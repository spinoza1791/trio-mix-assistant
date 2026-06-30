"""Self-signed certificate generation for the optional --https mode.

iOS/Android only enable Screen Wake Lock and a real PWA install over a secure
context (https). For a closed FOH LAN, a self-signed cert is enough — the
performer accepts it once on the tablet. The cert carries the laptop's LAN IP in
its SubjectAltName so Safari/Chrome accept it for that address.
"""
from __future__ import annotations

import ipaddress
import os


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def ensure_cert(certdir: str, hosts: list[str]) -> tuple[str, str]:
    """Return (certfile, keyfile), generating a self-signed pair if missing."""
    os.makedirs(certdir, exist_ok=True)
    certfile = os.path.join(certdir, "cert.pem")
    keyfile = os.path.join(certdir, "key.pem")
    if os.path.exists(certfile) and os.path.exists(keyfile):
        return certfile, keyfile
    try:
        _generate_cryptography(certfile, keyfile, hosts)
    except ImportError:
        _generate_openssl(certfile, keyfile, hosts)
    return certfile, keyfile


def _generate_cryptography(certfile: str, keyfile: str, hosts: list[str]) -> None:
    import datetime
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AutoFOH Mix-Assistant")])
    san = [x509.IPAddress(ipaddress.ip_address(h)) if _is_ip(h) else x509.DNSName(h)
           for h in hosts]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    with open(keyfile, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _generate_openssl(certfile: str, keyfile: str, hosts: list[str]) -> None:
    import subprocess
    san = ",".join((f"IP:{h}" if _is_ip(h) else f"DNS:{h}") for h in hosts)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", keyfile, "-out", certfile, "-days", "3650",
         "-subj", "/CN=AutoFOH Mix-Assistant", "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True,
    )
