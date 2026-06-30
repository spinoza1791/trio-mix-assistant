import os
import struct
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.capture import CaptureSource, SoundDeviceCapture
from trio_mix.engine import Engine
from trio_mix.metersrv import MeterReceiver, decode_meter_blob


class _FakeStream:
    def __init__(self, cb):
        self.cb = cb
    def start(self): pass
    def stop(self): pass
    def close(self): pass


def _const_frame(ncols):
    # each column j is filled with the value j, so we can verify channel mapping
    return np.tile(np.arange(ncols, dtype="float32"), (C.BLOCK, 1))


class TestSoundDeviceCapture(unittest.TestCase):
    def _cap(self):
        return SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb))

    def test_channel_mapping(self):
        cap = self._cap()
        cap.start()
        cap._on_audio(_const_frame(cap.ndev))
        out = cap.block()
        # console ch N -> device col N-1, filled with value N-1
        self.assertTrue(np.allclose(out[1], 0.0))
        self.assertTrue(np.allclose(out[2], 1.0))
        self.assertTrue(np.allclose(out[C.MEAS_MIC_CH], C.MEAS_MIC_CH - 1))
        self.assertEqual(out[1].shape[0], C.BLOCK)

    def test_drops_backlog_keeps_latest_and_counts_overruns(self):
        cap = self._cap()
        cap.start()
        for v in range(40):                       # exceed the queue (maxsize 32)
            cap._on_audio(np.full((C.BLOCK, cap.ndev), float(v), dtype="float32"))
        out = cap.block()                         # returns the most recent frame
        self.assertTrue(np.allclose(out[1], 39.0))
        self.assertGreater(cap._overruns, 0)

    def test_underrun_returns_silence_then_last(self):
        cap = self._cap(); cap.start()
        out = cap.block()                         # nothing fed yet -> silence
        self.assertTrue(np.allclose(out[1], 0.0))
        self.assertGreater(cap._underruns, 0)

    def test_capture_meas(self):
        cap = self._cap(); cap.start()
        for _ in range(3):
            cap._on_audio(_const_frame(cap.ndev))  # meas col = MEAS_MIC_CH-1
        rec = cap.capture_meas(0.05)               # ~2400 samples
        self.assertIsNotNone(rec)
        self.assertTrue(np.allclose(rec, C.MEAS_MIC_CH - 1))
        self.assertEqual(rec.size, int(0.05 * C.SAMPLE_RATE))


class TestMeterIngestion(unittest.TestCase):
    def test_decode_meter_blob(self):
        blob = struct.pack("<i", 3) + struct.pack("<3f", 0.5, 0.25, -1.0)
        self.assertEqual(decode_meter_blob(blob), [0.5, 0.25, -1.0])
        self.assertEqual(decode_meter_blob(b""), [])
        # garbled: claims 1000 floats but only 2 present -> clamped, no crash
        self.assertEqual(len(decode_meter_blob(struct.pack("<i", 1000) + struct.pack("<2f", 1, 2))), 2)

    def test_receiver_handlers(self):
        got = {}
        rx = MeterReceiver(on_fader=lambda ch, db: got.update(fader=(ch, db)),
                           on_meters=lambda v: got.update(meters=v))
        rx._running = True                               # simulate a started server
        rx.handle_fader("/ch/03/mix/fader", 0.75)        # 0.75 == 0 dB on X32
        self.assertEqual(got["fader"][0], 3)
        self.assertAlmostEqual(got["fader"][1], 0.0, delta=0.2)
        rx.handle_meters("/meters/1", struct.pack("<i", 2) + struct.pack("<2f", 0.5, 0.25))
        self.assertEqual(got["meters"], [0.5, 0.25])
        rx.handle_fader("/ch/zz/mix/fader", 0.5)         # malformed addr -> ignored, no raise

    def test_handlers_noop_after_stop(self):
        got = {}
        rx = MeterReceiver(on_fader=lambda ch, db: got.update(fader=1))
        rx._running = False                              # not started / already stopped
        rx.handle_fader("/ch/01/mix/fader", 0.5)         # straggler -> dropped
        self.assertNotIn("fader", got)


class TestReconciliation(unittest.TestCase):
    def test_external_move_adopted_self_move_ignored(self):
        e = Engine(sim=True)
        e.reconcile_fader(2, -6.0)                        # human moved it on the console
        self.assertAlmostEqual(e.assistant.fader_db[2], -6.0, places=1)
        self.assertGreater(e.assistant.manual_hold_until[2], 0)  # auto yields
        e.assistant.fader_db[4] = -3.0                   # our believed state
        e.reconcile_fader(4, -3.0)                        # echo of our own move -> ignore
        self.assertAlmostEqual(e.assistant.fader_db[4], -3.0, places=1)


class TestReconcileSelfMove(unittest.TestCase):
    def test_ignores_own_recent_move_adopts_later_external(self):
        e = Engine(sim=True)
        e.assistant.fader_db[2] = -2.0
        e.assistant.last_move_t[2] = time.monotonic()   # we just moved it (e.g. a ramp)
        e.reconcile_fader(2, -4.0)                       # echo within 0.8 s -> ignored
        self.assertAlmostEqual(e.assistant.fader_db[2], -2.0, places=1)
        e.assistant.last_move_t[2] = time.monotonic() - 1.0   # window elapsed
        e.reconcile_fader(2, -4.0)                       # genuine external move -> adopted
        self.assertAlmostEqual(e.assistant.fader_db[2], -4.0, places=1)


class TestDeadStream(unittest.TestCase):
    def test_dead_stream_emits_silence_not_frozen_frame(self):
        cap = SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb))
        cap.start()
        cap._on_audio(np.full((C.BLOCK, cap.ndev), 0.5, dtype="float32"))
        self.assertTrue(np.allclose(cap.block()[1], 0.5))     # got the frame
        out = None
        for _ in range(cap.SILENCE_AFTER + 2):                # starve it
            out = cap.block()
        self.assertTrue(np.allclose(out[1], 0.0))             # silence, not frozen 0.5
        self.assertTrue(cap.dead())
        self.assertTrue(cap.status()["dead"])


class _SilentSource(CaptureSource):
    def block(self):
        return {ch: np.zeros(C.BLOCK, "float32") for ch in C.CHANNELS}
    def capture_meas(self, seconds):
        return (1e-4 * np.ones(int(seconds * C.SAMPLE_RATE))).astype("float32")  # ~-80 dBFS


class TestCalibrationSilence(unittest.TestCase):
    def test_calibration_aborts_on_near_silence(self):
        e = Engine(sim=False, source=_SilentSource())
        try:
            e.run_calibration()
            time.sleep(1.7)
            self.assertEqual(e.calib_status, "none")
            self.assertEqual(e.status, "live")
            # aborts with an actionable message showing the (too-low) captured level
            self.assertTrue(any("needs > -50 dBFS" in ev["msg"] for ev in list(e.events)))
        finally:
            e.stop()


class _RingCapture(CaptureSource):
    """A non-sim source that emits a rising 2.5 kHz ring on the meas mic — proves
    an injected capture source drives the assistant in hardware mode."""
    def __init__(self):
        self.i = 0
        self.rng = np.random.default_rng(3)
    def block(self):
        t = (self.i * C.BLOCK + np.arange(C.BLOCK)) / C.SAMPLE_RATE
        self.i += 1
        frame = {ch: (0.004 * self.rng.standard_normal(C.BLOCK)).astype("float32")
                 for ch in C.CHANNELS}
        amp = min(0.5, 0.05 * (1.3 ** self.i))
        frame[C.MEAS_MIC_CH] = (0.004 * self.rng.standard_normal(C.BLOCK)
                                + amp * np.sin(2 * np.pi * 2500.0 * t)).astype("float32")
        return frame


class TestHardwarePathDrivesAssistant(unittest.TestCase):
    def test_injected_source_triggers_feedback(self):
        e = Engine(sim=False, source=_RingCapture(), tick=0.005)
        e.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if any(ev["kind"] == "feedback" for ev in list(e.events)):
                    break
                time.sleep(0.05)
            self.assertTrue(any(ev["kind"] == "feedback" for ev in list(e.events)),
                            "assistant did not catch feedback from the injected source")
        finally:
            e.stop()


if __name__ == "__main__":
    unittest.main()
