"""Dashboard server — stdlib HTTP + Server-Sent Events.

No web framework: telemetry is pushed over SSE (one-way, ideal for meters/log)
and operator actions come back as small JSON POSTs. Same-origin, so no CORS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine

# ---------------------------------------------------------------------------
# Limits + strict JSON (the server binds to the LAN — treat clients as untrusted)
# ---------------------------------------------------------------------------
_MAX_BODY = 1 << 16          # 64 KiB — control messages are tiny
_MAX_DRAIN = 1 << 18         # drain an over-cap body up to 256 KiB so the 400 isn't lost
                            # (larger declared bodies are closed without draining)
_WS_MAX_FRAME = 1 << 18      # 256 KiB hard cap per (assembled) WS message
_MAX_STREAMS = 32            # concurrent SSE+WS connections (thread-exhaustion guard)
_SOCK_TIMEOUT = 20.0         # seconds a stalled send may block before we drop a client


def _reject_const(token):
    raise ValueError(f"invalid JSON constant: {token}")


def _strict_loads(s: str):
    """json.loads that rejects NaN/Infinity (which JS JSON.parse can't read and
    which must never reach actuation)."""
    return json.loads(s, parse_constant=_reject_const)


def _json_for_html(obj) -> str:
    """json.dumps safe to inline inside an HTML <script> element.

    json.dumps does NOT escape '<', '>' or '&', so a snapshot string that
    contains '</script>' (e.g. an AbleSet song name arriving over unauthenticated
    LAN OSC) would close the inline <script> that seeds window.__INITIAL__ and let
    the rest run as HTML — a stored XSS on the control surface. Escaping these to
    their \\u00xx forms (plus the JS line separators U+2028/U+2029) neutralises the
    breakout; the result is still valid JSON/JS — the escapes decode to the same
    characters when the browser parses the object literal."""
    return (json.dumps(obj)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


# ---------------------------------------------------------------------------
# Minimal RFC-6455 WebSocket (stdlib only) — bidirectional control surface
# ---------------------------------------------------------------------------
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_recv(sock):
    """Return (fin, opcode, payload) or None on close / protocol violation
    (including an attacker-claimed frame length above the cap)."""
    h = _recv_exact(sock, 2)
    if not h:
        return None
    fin = h[0] & 0x80
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    if not masked:
        return None                      # RFC-6455: client frames MUST be masked
    ln = h[1] & 0x7F
    if ln == 126:
        e = _recv_exact(sock, 2)
        if e is None:
            return None
        ln = int.from_bytes(e, "big")
    elif ln == 127:
        e = _recv_exact(sock, 8)
        if e is None:
            return None
        ln = int.from_bytes(e, "big")
    if ln > _WS_MAX_FRAME:               # refuse to allocate an attacker-sized buffer
        return None
    mask = _recv_exact(sock, 4) if masked else b""
    if masked and mask is None:
        return None
    payload = _recv_exact(sock, ln) if ln else b""
    if payload is None:
        return None
    if masked and ln:                    # unmask via one C-level XOR, not a Python loop
        tiled = (mask * (ln // 4 + 1))[:ln]
        payload = (int.from_bytes(payload, "big") ^ int.from_bytes(tiled, "big")).to_bytes(ln, "big")
    return fin, opcode, payload


def _ws_send(sock, data, opcode=0x1):
    if isinstance(data, str):
        data = data.encode("utf-8")
    hdr = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr += n.to_bytes(2, "big")
    else:
        hdr.append(127)
        hdr += n.to_bytes(8, "big")
    sock.sendall(bytes(hdr) + data)

_STATIC = os.path.join(os.path.dirname(__file__), "static")

# Installable-PWA assets so the dashboard "Add to Home Screen"s as a full-screen
# app on iPad (Safari) and Android tablets (Chrome) alike — one codebase, both.
_MANIFEST = json.dumps({
    "name": "Trio AI Mix-Assistant",
    "short_name": "Mix-Assistant",
    "description": "Live operator dashboard for the Acoustic Trio AI Mix-Assistant.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "landscape",
    "background_color": "#0a0e14",
    "theme_color": "#0a0e14",
    "icons": [{"src": "/icon.svg", "sizes": "any",
               "type": "image/svg+xml", "purpose": "any maskable"}],
})

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#ff7a45"/><stop offset="1" stop-color="#2dd4bf"/></linearGradient></defs>
<rect width="512" height="512" rx="112" fill="#0a0e14"/>
<rect x="40" y="40" width="432" height="432" rx="84" fill="none" stroke="url(#g)" stroke-width="14" opacity="0.5"/>
<g stroke-linecap="round">
<line x1="150" y1="120" x2="150" y2="392" stroke="#222d3a" stroke-width="20"/>
<line x1="256" y1="120" x2="256" y2="392" stroke="#222d3a" stroke-width="20"/>
<line x1="362" y1="120" x2="362" y2="392" stroke="#222d3a" stroke-width="20"/>
<circle cx="150" cy="200" r="30" fill="#ff7a45"/>
<circle cx="256" cy="300" r="30" fill="#2dd4bf"/>
<circle cx="362" cy="170" r="30" fill="#f6c453"/>
</g></svg>"""


def _read_index() -> str:
    with open(os.path.join(_STATIC, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


def _host_allowed(host_header: str) -> bool:
    """DNS-rebinding guard: legitimate access to this LAN tool is always by IP
    literal, `localhost`, or an mDNS `*.local` name — never a public domain. A
    real hostname in the Host header means a browser was pointed at an attacker
    domain that rebound to our LAN IP, so reject it. Empty Host (odd client) is
    tolerated; the app itself only ever advertises IP URLs."""
    h = (host_header or "").strip()
    if not h:
        return True
    if h.startswith("["):                        # [IPv6]:port
        h = h[1:h.index("]")] if "]" in h else h[1:]
    elif h.count(":") == 1:                       # host:port (a bare IPv6 has >1 colon)
        h = h.rsplit(":", 1)[0]
    hl = h.lower()
    if hl == "localhost" or hl.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


# Minimal PIN login page (served for GET / when a --pin is set and the client
# isn't authenticated yet). Self-contained; posts the PIN and reloads on success.
LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Mix-Assistant — locked</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;height:100vh;display:grid;place-items:center;background:#0a0e14;
 color:#e6edf3;font:16px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
.card{width:min(92vw,340px);padding:28px 26px;border:1px solid #1c2733;border-radius:16px;
 background:#0e141c;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.5)}
h1{font-size:18px;margin:0 0 4px}p{color:#8aa0b2;font-size:13px;margin:0 0 18px}
input{width:100%;padding:13px;font-size:20px;text-align:center;letter-spacing:.3em;
 border:1px solid #26323f;border-radius:10px;background:#0a0f16;color:#e6edf3}
button{width:100%;margin-top:12px;padding:12px;font-size:15px;font-weight:700;border:0;
 border-radius:10px;cursor:pointer;color:#04121e;background:linear-gradient(180deg,#8fd0ff,#5db4ff)}
.err{color:#ff9ba3;font-size:13px;min-height:18px;margin-top:10px}
</style></head><body><form class="card" id="f">
<h1>🎚️ Mix-Assistant</h1><p>Enter the access PIN to continue</p>
<input id="pin" type="password" inputmode="numeric" autocomplete="one-time-code"
 autofocus aria-label="PIN"><button>Unlock</button><div class="err" id="err"></div>
</form><script>
const f=document.getElementById("f"),pin=document.getElementById("pin"),err=document.getElementById("err");
f.onsubmit=async e=>{e.preventDefault();err.textContent="";
 let r;try{r=await fetch("/api/login",{method:"POST",headers:{"content-type":"application/json"},
  body:JSON.stringify({pin:pin.value})});}catch(_){err.textContent="Network error.";return;}
 if(r.ok){location.href="/";}else{err.textContent=r.status===429?"Too many tries — wait a moment.":"Wrong PIN.";pin.value="";pin.focus();}};
</script></body></html>"""


def make_handler(engine: Engine, pin: str | None = None, ca_pem: str | None = None):
    streams = threading.Semaphore(_MAX_STREAMS)   # cap long-lived SSE+WS threads
    pin = str(pin) if pin not in (None, "") else None   # None -> auth disabled
    token = secrets.token_urlsafe(32) if pin else None  # per-process session bearer
    auth = {"fails": 0, "lock_until": 0.0}
    auth_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 15            # bound request-read (headers + body + drain) vs slow-loris;
                                # SSE/WS override this with their own longer send timeout

        def log_message(self, *args):       # keep the console quiet
            pass

        # -- helpers --------------------------------------------------------
        def _send_json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text, ctype, code=200):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _drain(self, n: int) -> None:
            """Read and discard up to n body bytes so a rejected request doesn't
            leave bytes in the socket (which would RST and lose our response)."""
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n < 0:
                raise ValueError("bad Content-Length")   # malformed -> 400 + close
            if n == 0:
                return {}
            if n > _MAX_BODY:
                if n <= _MAX_DRAIN:          # drain a merely-too-big body so the 400 lands
                    self._drain(n)
                raise ValueError("request body too large")
            obj = _strict_loads(self.rfile.read(n).decode("utf-8") or "{}")
            return obj if isinstance(obj, dict) else {}

        # -- auth / host ----------------------------------------------------
        def _host_ok(self) -> bool:
            return _host_allowed(self.headers.get("Host", ""))

        def _authed(self) -> bool:
            """True if auth is disabled (no PIN) or the request carries the valid
            session cookie (constant-time compared)."""
            if pin is None:
                return True
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "mixauth":
                    return hmac.compare_digest(v, token or "")
            return False

        def _secure(self) -> bool:
            import ssl
            return isinstance(self.connection, ssl.SSLSocket)

        def _do_login(self):
            now = time.monotonic()
            with auth_lock:
                if now < auth["lock_until"]:
                    return self.send_error(429)      # locked out after repeated fails
            try:
                body = self._read_body()
            except (ValueError, UnicodeDecodeError, RecursionError):
                self.close_connection = True
                return self.send_error(400)
            ok = pin is not None and hmac.compare_digest(str(body.get("pin", "")), pin)
            if not ok:
                with auth_lock:
                    auth["fails"] += 1
                    if auth["fails"] >= 5:           # 5 tries -> 30 s cooldown
                        auth["fails"], auth["lock_until"] = 0, now + 30.0
                time.sleep(0.3)                      # blunt online brute force
                return self._send_json({"ok": False}, code=401)
            with auth_lock:
                auth["fails"], auth["lock_until"] = 0, 0.0
            out = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            cookie = f"mixauth={token}; HttpOnly; SameSite=Strict; Path=/"
            if self._secure():
                cookie += "; Secure"                 # only over TLS (else the browser drops it)
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(out)

        # -- GET ------------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return self.send_error(403)
            path = self.path.split("?", 1)[0].split("#", 1)[0]   # ignore query/hash for routing
            if path == "/" or path.startswith("/index"):
                if not self._authed():
                    return self._send_text(LOGIN_HTML, "text/html; charset=utf-8")
                html = _read_index().replace(
                    "__INITIAL_STATE__", _json_for_html(engine.snapshot()))
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/state":
                if not self._authed():
                    return self.send_error(401)
                self._send_json(engine.snapshot())
            elif path == "/events":
                if not self._authed():
                    return self.send_error(401)
                self._serve_sse()
            elif path == "/ws":
                if not self._authed():
                    return self.send_error(401)
                self._serve_ws()
            elif path == "/manifest.webmanifest":
                self._send_text(_MANIFEST, "application/manifest+json")
            elif path == "/icon.svg":
                self._send_text(_ICON_SVG, "image/svg+xml")
            elif path in ("/ca.pem", "/ca.crt") and ca_pem:
                # The public root CA, for one-tap install on a device (no auth —
                # a CA certificate is public). iOS treats this MIME as a profile.
                body = ca_pem.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-x509-ca-cert")
                self.send_header("Content-Disposition", 'attachment; filename="MixAssistant-CA.pem"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def _serve_sse(self):
            if not streams.acquire(blocking=False):
                return self.send_error(503)        # too many live connections
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.connection.settimeout(_SOCK_TIMEOUT)   # drop a stalled reader
                while True:
                    payload = json.dumps(engine.snapshot())
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                return                              # client gone / stalled / reset
            finally:
                streams.release()

        def _serve_ws(self):
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                return self.send_error(400)
            if not streams.acquire(blocking=False):
                return self.send_error(503)
            try:
                # The handshake itself is inside the try so a failure during it
                # (client vanishes mid-upgrade) still releases the semaphore slot.
                self.close_connection = True
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
                self.end_headers()
                self.wfile.flush()
                sock = self.connection
                sock.settimeout(_SOCK_TIMEOUT)      # bound any blocking send/recv
                last = 0.0
                frag = bytearray()
                frag_op = None
                _ws_send(sock, json.dumps(engine.snapshot()))     # initial state
                while True:
                    r, _, _ = select.select([sock], [], [], 0.1)
                    if r:
                        msg = _ws_recv(sock)
                        if msg is None:
                            break                   # close / oversize / protocol error
                        fin, opcode, payload = msg
                        if opcode == 0x8:                          # close
                            break
                        if opcode == 0x9:                          # ping -> pong
                            _ws_send(sock, payload, opcode=0xA)
                            continue
                        if opcode == 0xA:                          # pong
                            continue
                        if opcode == 0x0:                          # continuation
                            if frag_op is None:
                                break               # continuation with no start
                            frag += payload
                        elif opcode in (0x1, 0x2):                 # text / binary start
                            frag = bytearray(payload)
                            frag_op = opcode
                        else:
                            continue
                        if len(frag) > _WS_MAX_FRAME:
                            break                   # assembled message too large
                        if fin and frag_op is not None:
                            data, op = bytes(frag), frag_op
                            frag, frag_op = bytearray(), None
                            if op == 0x1 and data:
                                try:
                                    engine.command(_strict_loads(data.decode("utf-8")))
                                except (ValueError, KeyError, TypeError,
                                        UnicodeDecodeError, RecursionError):
                                    pass
                    now = time.monotonic()
                    if now - last >= 0.1:
                        _ws_send(sock, json.dumps(engine.snapshot()))
                        last = now
            except OSError:
                return
            finally:
                streams.release()

        # -- POST -----------------------------------------------------------
        def do_POST(self):
            if not self._host_ok():
                return self.send_error(403)
            if self.path == "/api/login":
                return self._do_login()
            if not self._authed():
                return self.send_error(401)
            try:
                body = self._read_body()
            except (ValueError, UnicodeDecodeError, RecursionError):
                # A rejected body may not have been fully read off the socket;
                # close the connection so leftover bytes can't desync keep-alive.
                self.close_connection = True
                return self.send_error(400)
            try:
                if self.path == "/api/toggle":
                    engine.set_enabled(str(body.get("job")), bool(body.get("on")))
                elif self.path == "/api/panic":
                    engine.panic(bool(body.get("on")))
                elif self.path == "/api/calibrate":
                    engine.run_calibration()
                elif self.path == "/api/lead_target":
                    engine.set_lead_target(body.get("db", engine.assistant.lead_target))
                elif self.path == "/api/reset_notches":
                    engine.reset_notches()
                elif self.path == "/api/balance_capture":
                    engine.capture_balance_now()
                elif self.path == "/api/scene":
                    engine.recall_scene_manual(body.get("index"))
                elif self.path == "/api/command":
                    engine.command(body)
                else:
                    return self.send_error(404)
            except (ValueError, TypeError, KeyError):
                return self.send_error(400)
            self._send_json(engine.snapshot())

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a SECOND instance bind the same port too (a
    # silent dual-bind), so a double-launch wouldn't error. Bind EXCLUSIVELY there
    # so a real port clash raises (clean "port in use" message). On POSIX keep
    # reuse on for TIME_WAIT-friendly restarts (an active clash still raises).
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        if os.name == "nt":
            import socket as _socket
            if hasattr(_socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    self.socket.setsockopt(_socket.SOL_SOCKET,
                                           _socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
        super().server_bind()

    def handle_error(self, request, client_address):
        # SSE/WS clients disconnecting mid-stream — and plain-HTTP clients
        # hitting the TLS port — are normal; don't spam tracebacks.
        import ssl
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, TimeoutError, ssl.SSLError)):
            return                          # incl. a stalled slow-loris read timing out
        super().handle_error(request, client_address)


def serve(engine: Engine, host: str = "127.0.0.1", port: int = 8770,
          pin: str | None = None, ca_pem: str | None = None):
    return _QuietServer((host, port), make_handler(engine, pin, ca_pem))
