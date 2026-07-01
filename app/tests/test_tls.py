import ipaddress
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.tls import ensure_cert


def _lifetime_days(c):
    import datetime
    nb = getattr(c, "not_valid_before_utc", None) or c.not_valid_before
    na = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
    return (na - nb).days


def _write_overlong(certfile, keyfile, days=3650):
    """Write a self-signed cert with an over-long validity (the old 10-year bug)."""
    import datetime
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "old")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    with open(keyfile, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


class TestTLS(unittest.TestCase):
    def test_cert_is_browser_acceptable(self):
        d = tempfile.mkdtemp(prefix="autofoh_cert_")
        try:
            cert, _ = ensure_cert(d, ["127.0.0.1"])
            from cryptography import x509
            from cryptography.x509.oid import ExtendedKeyUsageOID
            c = x509.load_pem_x509_certificate(open(cert, "rb").read())
            self.assertLessEqual(_lifetime_days(c), 398)          # under the browser cap
            eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, eku)   # a serverAuth leaf...
            bc = c.extensions.get_extension_for_class(x509.BasicConstraints).value
            self.assertFalse(bc.ca)                               # ...not a CA cert
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_overlong_cached_cert_is_regenerated(self):
        d = tempfile.mkdtemp(prefix="autofoh_cert_")
        try:
            cert, key = ensure_cert(d, ["127.0.0.1"])
            _write_overlong(cert, key)                            # simulate the old 10-yr cert
            from cryptography import x509
            c0 = x509.load_pem_x509_certificate(open(cert, "rb").read())
            self.assertGreater(_lifetime_days(c0), 398)           # confirm it's the bad one
            ensure_cert(d, ["127.0.0.1"])                         # must detect + replace
            c1 = x509.load_pem_x509_certificate(open(cert, "rb").read())
            self.assertLessEqual(_lifetime_days(c1), 398)         # regenerated short-lived
        finally:
            shutil.rmtree(d, ignore_errors=True)
    def test_generate_with_lan_ip_in_san(self):
        d = tempfile.mkdtemp(prefix="autofoh_cert_")
        try:
            cert, key = ensure_cert(d, ["localhost", "127.0.0.1", "192.168.1.50"])
            self.assertTrue(os.path.exists(cert) and os.path.exists(key))

            # idempotent: a second call reuses the same files
            cert2, _ = ensure_cert(d, ["localhost"])
            self.assertEqual(cert, cert2)

            from cryptography import x509
            with open(cert, "rb") as f:
                c = x509.load_pem_x509_certificate(f.read())
            san = c.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            ips = san.get_values_for_type(x509.IPAddress)
            self.assertIn(ipaddress.ip_address("192.168.1.50"), ips)
            self.assertIn(ipaddress.ip_address("127.0.0.1"), ips)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
