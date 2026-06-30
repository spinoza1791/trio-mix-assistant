import ipaddress
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.tls import ensure_cert


class TestTLS(unittest.TestCase):
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
