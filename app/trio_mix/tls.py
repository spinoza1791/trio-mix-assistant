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


# Browsers reject a leaf cert valid for more than ~398 days with
# NET::ERR_CERT_VALIDITY_TOO_LONG (Chrome) / a hard fail on iOS Safari — and those
# error types HIDE the "proceed anyway" button, so an over-long self-signed cert
# is un-bypassable. Stay under the cap so the one-time warning can be accepted.
# (Total validity span = CERT_DAYS + 1 for the 1-day backdate, so 396 -> 397 days.)
CERT_DAYS = 396


def ensure_cert(certdir: str, hosts: list[str]) -> tuple[str, str]:
    """Return (certfile, keyfile), generating a self-signed pair if missing or if
    the cached one is stale/over-long (see _cert_is_current)."""
    os.makedirs(certdir, exist_ok=True)
    certfile = os.path.join(certdir, "cert.pem")
    keyfile = os.path.join(certdir, "key.pem")
    if os.path.exists(certfile) and os.path.exists(keyfile) and _cert_is_current(certfile):
        return certfile, keyfile
    try:
        _generate_cryptography(certfile, keyfile, hosts)
    except ImportError:
        _generate_openssl(certfile, keyfile, hosts)
    try:
        os.chmod(keyfile, 0o600)          # private key: owner-only (best-effort on Windows)
    except OSError:
        pass
    return certfile, keyfile


def _cert_is_current(certfile: str) -> bool:
    """True only if the cached cert is still browser-acceptable. Regenerate when
    it is valid for > ~398 days (would trip ERR_CERT_VALIDITY_TOO_LONG / iOS hard
    fail), is expired / within 30 days of expiry, or isn't a serverAuth leaf —
    this auto-heals the old 10-year certs that couldn't be bypassed. If
    cryptography isn't importable to inspect it, keep the existing file."""
    try:
        import datetime
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID
        with open(certfile, "rb") as f:
            c = x509.load_pem_x509_certificate(f.read())
        nb = getattr(c, "not_valid_before_utc", None) or \
            c.not_valid_before.replace(tzinfo=datetime.timezone.utc)
        na = getattr(c, "not_valid_after_utc", None) or \
            c.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        if (na - nb).days > 398:                       # over-long -> un-bypassable
            return False
        if na - now < datetime.timedelta(days=30):     # expired / about to
            return False
        eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        return ExtendedKeyUsageOID.SERVER_AUTH in eku
    except ImportError:
        return True                                    # can't inspect -> don't churn
    except Exception:
        return False                                   # unreadable/no-EKU -> regenerate


def _generate_cryptography(certfile: str, keyfile: str, hosts: list[str]) -> None:
    import datetime
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
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
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        # A proper TLS *leaf* (not a CA) with serverAuth EKU — browsers accept the
        # one-time warning; a CA-basic-constraints cert used for TLS is refused by some.
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=True, content_commitment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
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
         "-keyout", keyfile, "-out", certfile, "-days", str(CERT_DAYS),
         "-subj", "/CN=AutoFOH Mix-Assistant",
         "-addext", f"subjectAltName={san}",
         "-addext", "basicConstraints=critical,CA:FALSE",
         "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
         "-addext", "extendedKeyUsage=serverAuth"],
        check=True, capture_output=True,
    )
