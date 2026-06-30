import base64
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.engine import Engine
from trio_mix.server import serve


# -- minimal WebSocket client for the smoke test ----------------------------
def _ws_connect(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(b"0123456789abcdef").decode()
    s.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        data += s.recv(1024)
    assert b" 101 " in data.split(b"\r\n")[0], data[:64]
    return s


def _ws_recv_n(s, n):
    b = b""
    while len(b) < n:
        b += s.recv(n - len(b))
    return b


def _ws_read(s):                       # server frames are unmasked
    h = _ws_recv_n(s, 2)
    ln = h[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(_ws_recv_n(s, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_ws_recv_n(s, 8), "big")
    return _ws_recv_n(s, ln)


def _ws_send(s, text):                  # client frames must set the mask bit
    payload = text.encode()
    s.sendall(bytes([0x81, 0x80 | len(payload)]) + b"\x00\x00\x00\x00" + payload)


def _ws_send_frag(s, payload, fin, opcode):
    b0 = (0x80 if fin else 0x00) | opcode
    s.sendall(bytes([b0, 0x80 | len(payload)]) + b"\x00\x00\x00\x00" + payload)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(sim=True)          # no background loop needed
        # Bind to port 0 and read the OS-assigned port — no find-then-bind race.
        cls.httpd = serve(cls.engine, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.engine.stop()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def _post(self, path, body):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_index_html(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Mix-Assistant", body)
        self.assertIn("window.__INITIAL__", body)
        self.assertNotIn("__INITIAL_STATE__", body)     # token was substituted

    def test_state_json(self):
        status, body = self._get("/state")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("status", "enabled", "channels", "calibration", "log"):
            self.assertIn(key, data)
        self.assertEqual(len(data["channels"]), 8)

    def test_toggle_endpoint(self):
        status, data = self._post("/api/toggle", {"job": "vocal_ride", "on": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"]["vocal_ride"])
        _, data2 = self._post("/api/toggle", {"job": "vocal_ride", "on": False})
        self.assertFalse(data2["enabled"]["vocal_ride"])

    def test_panic_endpoint(self):
        _, data = self._post("/api/panic", {"on": True})
        self.assertEqual(data["status"], "takeover")
        self.assertTrue(data["main_muted"])
        _, data2 = self._post("/api/panic", {"on": False})
        self.assertEqual(data2["status"], "live")

    def test_lead_target_endpoint(self):
        _, data = self._post("/api/lead_target", {"db": -9.5})
        self.assertAlmostEqual(data["lead"]["target"], -9.5, places=1)

    def test_pwa_assets(self):
        status, body = self._get("/manifest.webmanifest")
        self.assertEqual(status, 200)
        man = json.loads(body)
        self.assertEqual(man["display"], "standalone")
        self.assertTrue(man["icons"])
        istatus, isvg = self._get("/icon.svg")
        self.assertEqual(istatus, 200)
        self.assertIn("<svg", isvg)

    def test_command_endpoint(self):
        status, data = self._post("/api/command", {"type": "mute", "ch": 3, "on": True})
        self.assertEqual(status, 200)
        ch3 = next(c for c in data["channels"] if c["ch"] == 3)
        self.assertTrue(ch3["muted"])

    def test_websocket_telemetry_and_control(self):
        s = _ws_connect(self.port)
        try:
            first = json.loads(_ws_read(s).decode("utf-8"))   # initial snapshot
            self.assertEqual(len(first["channels"]), 8)
            _ws_send(s, json.dumps({"type": "fader", "ch": 1, "db": -7.0}))
            time.sleep(0.3)
            self.assertAlmostEqual(self.engine.assistant.fader_db[1], -7.0, places=1)
        finally:
            s.close()

    def test_oversized_body_rejected(self):
        import urllib.error
        big = json.dumps({"type": "toggle", "job": "x", "on": True,
                          "pad": "A" * 70000}).encode()
        req = urllib.request.Request(self.base + "/api/command", data=big,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_nan_body_rejected(self):
        import urllib.error
        req = urllib.request.Request(self.base + "/api/command",
                                     data=b'{"type":"fader","ch":1,"db":NaN}',
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_deeply_nested_json_rejected(self):
        import urllib.error
        body = b"[" * 10000 + b"]" * 10000             # RecursionError on parse (~20 KB)
        req = urllib.request.Request(self.base + "/api/command", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_unmasked_ws_frame_closes_connection(self):
        s = _ws_connect(self.port)
        try:
            _ws_read(s)                                 # initial snapshot
            payload = b'{"type":"fader","ch":2,"db":-3}'
            s.sendall(bytes([0x81, len(payload)]) + payload)   # NO mask -> RFC violation
            s.settimeout(2.5)
            closed = False
            try:
                while True:
                    if not s.recv(4096):                # server closed the connection
                        closed = True
                        break
            except socket.timeout:
                closed = False
            self.assertTrue(closed, "server did not close on an unmasked frame")
        finally:
            s.close()

    def test_websocket_fragmented_message(self):
        s = _ws_connect(self.port)
        try:
            _ws_read(s)                                  # initial snapshot
            payload = json.dumps({"type": "fader", "ch": 2, "db": -5.0}).encode()
            mid = len(payload) // 2
            _ws_send_frag(s, payload[:mid], fin=False, opcode=0x1)   # start
            _ws_send_frag(s, payload[mid:], fin=True, opcode=0x0)    # continuation+FIN
            time.sleep(0.3)
            self.assertAlmostEqual(self.engine.assistant.fader_db[2], -5.0, places=1)
        finally:
            s.close()

    def test_sse_stream_yields_frame(self):
        with urllib.request.urlopen(self.base + "/events", timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/event-stream", r.headers.get("Content-Type", ""))
            chunk = r.read(64)                  # first bytes of the first frame
            self.assertTrue(chunk.startswith(b"data: "))


if __name__ == "__main__":
    unittest.main()
