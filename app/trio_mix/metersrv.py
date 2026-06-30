"""OSC ingestion from the console: meters + parameter reconciliation.

After a `/batchsubscribe` (+ `/xremote`) the M32C streams `/meters/N` level
blobs and pushes every parameter change (e.g. `/ch/01/mix/fader <pos>`) to us.
We listen on LOCAL_PORT and:

  * decode meter blobs -> latest per-channel console levels (a redundant readout);
  * decode `/ch/NN/mix/fader` pushes -> reconcile the believed fader position, so
    a human moving a fader on the console/iPad is detected and adopted (the
    shared-control reconciliation the design calls for).

The OSC server needs python-osc; the blob decoder and the handlers are plain
functions so they're unit-testable without any console.
"""
from __future__ import annotations

import struct
import threading

from . import config as C


def decode_meter_blob(blob: bytes) -> list[float]:
    """X32/M32 meter blob: little-endian int32 count, then `count` float32 (LE).
    Defensive against a short/garbled blob (clamps the count to what's present)."""
    if not blob or len(blob) < 4:
        return []
    n = struct.unpack_from("<i", blob, 0)[0]
    n = max(0, min(n, (len(blob) - 4) // 4))
    if n == 0:
        return []
    return list(struct.unpack_from("<%df" % n, blob, 4))


def encode_meter_blob(values) -> bytes:
    """Inverse of decode_meter_blob — used by the desk emulator to produce a
    byte-for-byte `/meters` blob in the SAME layout the decoder assumes.
    NB: this layout (LE int32 count + LE float32 values) is OUR ASSUMPTION about
    the X32/M32 `/meters/1` bank; VERIFY it against a real desk capture or the
    published X32 OSC protocol before trusting it live (see HARDWARE_BRINGUP.md)."""
    vals = [float(v) for v in values]
    return struct.pack("<i", len(vals)) + struct.pack("<%df" % len(vals), *vals)


def _ch_from_fader_addr(addr: str) -> int | None:
    """/ch/NN/mix/fader -> NN (int) or None."""
    parts = addr.split("/")
    if len(parts) >= 5 and parts[1] == "ch" and parts[3] == "mix" and parts[4] == "fader":
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None


class MeterReceiver:
    def __init__(self, on_fader=None, on_meters=None,
                 ip: str = "0.0.0.0", port: int = C.LOCAL_PORT) -> None:
        self.on_fader = on_fader          # callback(ch:int, db:float)
        self.on_meters = on_meters        # callback(list[float])
        self.ip, self.port = ip, port
        self.server = None
        self._running = False
        self.latest_meters: list[float] = []

    # -- OSC handlers (called by python-osc as handler(address, *args)) ------
    def handle_meters(self, address, *args) -> None:
        if not self._running:                 # straggler after stop() -> drop it
            return
        if args and isinstance(args[0], (bytes, bytearray)):
            vals = decode_meter_blob(bytes(args[0]))
            self.latest_meters = vals
            if self.on_meters:
                self.on_meters(vals)

    def handle_fader(self, address, *args) -> None:
        if not self._running:                 # straggler after stop() -> drop it
            return
        ch = _ch_from_fader_addr(address)
        if ch is None or not args:
            return
        try:
            pos = float(args[0])
        except (TypeError, ValueError):
            return
        from .osc import fader_to_db
        if self.on_fader:
            self.on_fader(ch, fader_to_db(pos))

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:                              # pragma: no cover - needs python-osc bind
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
        disp = Dispatcher()
        disp.map("/meters*", self.handle_meters)
        disp.map("/ch/*/mix/fader", self.handle_fader)
        disp.set_default_handler(lambda *a: None)         # ignore everything else
        self.server = ThreadingOSCUDPServer((self.ip, self.port), disp)
        self._running = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._running = False                 # fence: in-flight handlers become no-ops
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
