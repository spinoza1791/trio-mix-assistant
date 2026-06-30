"""Contract tests — pin the X32/M32 wire layout the app assumes, so a future
change can't silently drift, and so there's ONE place listing exactly what must
be confirmed against real hardware.

Every value tagged `VERIFY:` is OUR ASSUMPTION about the desk's protocol. These
tests prove the app is *self-consistent* with that assumption; they do NOT prove
the assumption matches your firmware. To make them validating, replace the
expected constants below with bytes/values from a real-desk capture or the
published X32 OSC protocol (Maillot), then anything that drifts will fail here.
See HARDWARE_BRINGUP.md.
"""
import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.metersrv import decode_meter_blob, encode_meter_blob
from trio_mix.osc import SimConsole, db_to_fader, fader_to_db

try:
    import pythonosc  # noqa: F401
    HAVE_OSC = True
except ImportError:
    HAVE_OSC = False


def _wire(con):
    """Map the raw (addr, *args) the console emitted -> {addr: args-or-arg}."""
    out = {}
    for entry in con.wire_log:
        out[entry[0]] = entry[1] if len(entry) == 2 else tuple(entry[1:])
    return out


class TestWireContract(unittest.TestCase):
    # ---- fader scaling: linear 0..1, piecewise (VERIFY against firmware) ----
    def test_fader_scaling_points(self):
        # VERIFY: 0 dB -> 0.75, +10 -> 1.0, -10 -> 0.5, -30 -> 0.25,
        #         -60 -> 0.0625, -inf -> 0.0   (X32 linear fader law)
        self.assertAlmostEqual(db_to_fader(0.0), 0.75, places=6)
        self.assertAlmostEqual(db_to_fader(10.0), 1.0, places=6)
        self.assertAlmostEqual(db_to_fader(-10.0), 0.5, places=6)
        self.assertAlmostEqual(db_to_fader(-30.0), 0.25, places=6)
        self.assertAlmostEqual(db_to_fader(-60.0), 0.0625, places=6)
        self.assertEqual(db_to_fader(-90.0), 0.0)

    def test_fader_roundtrip_exact(self):
        for db in (-60, -30, -12, -6, 0, 5, 10):
            self.assertAlmostEqual(fader_to_db(db_to_fader(db)), db, places=4)

    # ---- /meters blob layout: LE int32 count + LE float32 values ----
    def test_meter_blob_layout(self):
        blob = encode_meter_blob([0.5, 0.25, -1.0])
        # VERIFY: exact bytes — count=3 (int32 LE) then 3 float32 LE
        self.assertEqual(blob[:4], struct.pack("<i", 3))
        self.assertEqual(blob, struct.pack("<i", 3) + struct.pack("<3f", 0.5, 0.25, -1.0))
        self.assertEqual(decode_meter_blob(blob), [0.5, 0.25, -1.0])

    def test_meter_blob_roundtrip(self):
        vals = [0.0, 0.1, 0.9, 1.0, -0.5]
        self.assertEqual([round(x, 4) for x in decode_meter_blob(encode_meter_blob(vals))],
                         [round(x, 4) for x in vals])

    # ---- OSC addresses + value scalings (recorded raw wire traffic) ----
    def test_fader_address_and_value(self):
        con = SimConsole()
        con.set_fader_raw(3, 0.0)
        w = _wire(con)
        self.assertIn("/ch/03/mix/fader", w)            # VERIFY: /ch/NN/mix/fader
        self.assertAlmostEqual(w["/ch/03/mix/fader"], 0.75, places=6)

    def test_headamp_address_and_value(self):
        con = SimConsole()
        con.nudge_gain_db(1, 20.0, 0.0)                  # ch1 -> headamp index 000
        w = _wire(con)
        # VERIFY: /headamp/NNN/gain (index = ch-1, 3 digits), value = (dB+12)/72
        self.assertIn("/headamp/000/gain", w)
        self.assertAlmostEqual(w["/headamp/000/gain"], (20.0 + 12.0) / 72.0, places=6)

    def test_eq_notch_addresses(self):
        con = SimConsole()
        con.set_eq_notch(2, 1, 1000.0, gain_db=-12.0, q=8.0)
        w = _wire(con)
        # VERIFY: PEQ type=2, /f log-scaled, /g=(dB+15)/30, /q, channel eq on
        self.assertEqual(w["/ch/02/eq/1/type"], 2)
        self.assertAlmostEqual(w["/ch/02/eq/1/g"], (-12.0 + 15.0) / 30.0, places=6)
        self.assertEqual(w["/ch/02/eq/on"], 1)

    def test_scene_and_mute_addresses(self):
        con = SimConsole()
        con.recall_scene(5)
        con.set_channel_mute(4, True)
        w = _wire(con)
        self.assertEqual(w["/-action/goscene"], 5)       # VERIFY: scene recall addr
        self.assertEqual(w["/ch/04/mix/on"], 0)          # VERIFY: 0 == muted

    def test_subscribe_handshake(self):
        con = SimConsole()
        con.subscribe_meters()
        w = _wire(con)
        # VERIFY: batchsubscribe args + the separate /xremote that drives reconciliation
        self.assertEqual(w["/batchsubscribe"], ("/meters", "/meters/1", 0, 0, 10))
        self.assertIn("/xremote", w)


# ---------------------------------------------------------------------------
# Full real-socket integration against the desk emulator
# ---------------------------------------------------------------------------
def _free_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(HAVE_OSC, "python-osc not installed")
class TestEmulatorOverSockets(unittest.TestCase):
    def setUp(self):
        from trio_mix.emulator import EmulatedDesk
        from trio_mix.metersrv import MeterReceiver
        from trio_mix.osc import OscConsole
        self.console_port = _free_port()
        self.app_port = _free_port()
        self.faders, self.meters = [], []
        self.rx = MeterReceiver(on_fader=lambda ch, db: self.faders.append((ch, db)),
                                on_meters=lambda v: self.meters.append(v),
                                port=self.app_port)
        self.emu = EmulatedDesk(listen_port=self.console_port, app_port=self.app_port,
                                meter_hz=40.0)
        self.con = OscConsole("127.0.0.1", port=self.console_port)
        self.rx.start()
        self.emu.start()

    def tearDown(self):
        self.rx.stop()
        self.emu.stop()
        self.con.close()

    def _wait(self, cond, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_command_reaches_desk_and_echoes(self):
        self.con.subscribe_meters()                       # enable /xremote echo
        self.assertTrue(self._wait(lambda: self.emu.subscribed))
        self.con.set_fader_raw(4, -5.0)                   # app commands a fader
        self.assertTrue(self._wait(lambda: 4 in self.emu.fader))
        self.assertAlmostEqual(self.emu.fader[4], db_to_fader(-5.0), places=5)
        # the desk echoed it back to us via /xremote
        self.assertTrue(self._wait(lambda: any(ch == 4 for ch, _ in self.faders)))

    def test_meters_stream_decoded(self):
        self.con.subscribe_meters()
        self.assertTrue(self._wait(lambda: len(self.meters) >= 2))
        self.assertTrue(all(isinstance(v, list) and v for v in self.meters))

    def test_external_move_is_reconciled(self):
        self.con.subscribe_meters()
        self.assertTrue(self._wait(lambda: self.emu.subscribed))
        self.emu.move_fader_externally(2, -6.0)           # a human moves a desk fader
        self.assertTrue(self._wait(lambda: any(ch == 2 for ch, _ in self.faders)))
        db = [d for ch, d in self.faders if ch == 2][-1]
        self.assertAlmostEqual(db, -6.0, delta=0.3)

    def test_scene_recall_round_trip(self):
        self.con.recall_scene(7)
        self.assertTrue(self._wait(lambda: self.emu.scene == 7))


if __name__ == "__main__":
    unittest.main()
