import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.assistant import MixAssistant
from trio_mix.dsp import ChannelFeatures
from trio_mix.engine import Engine
from trio_mix.osc import SimConsole


def feat(rms=-30.0, peak=-20.0, fb=None, contrast=None):
    if contrast is None:
        contrast = 24.0 if fb else 6.0     # a real ring is high-contrast (peak >> floor)
    return ChannelFeatures(rms_dbfs=rms, peak_dbfs=peak, fb_freq=fb, contrast_db=contrast)


class TestManualEngine(unittest.TestCase):
    def setUp(self):
        self.e = Engine(sim=True)          # no loop needed

    def test_manual_fader(self):
        self.e.set_fader(1, -3.0)
        self.assertAlmostEqual(self.e.assistant.fader_db[1], -3.0, places=1)
        self.assertAlmostEqual(self.e.con.fader_db[1], -3.0, places=1)

    def test_manual_fader_full_range(self):
        # human moves are not bound by the AI guardrails (-12..+6)
        self.e.set_fader(2, -40.0)
        self.assertAlmostEqual(self.e.con.fader_db[2], -40.0, places=1)
        self.e.set_fader(2, 9.0)
        self.assertAlmostEqual(self.e.con.fader_db[2], 9.0, places=1)

    def test_mute(self):
        self.e.set_mute(3, True)
        self.assertTrue(self.e.muted[3])
        self.assertTrue(self.e.con.ch_muted[3])
        self.e.set_mute(3, False)
        self.assertFalse(self.e.muted[3])

    def test_master(self):
        self.e.set_master(-4.0)
        self.assertAlmostEqual(self.e.master_db, -4.0, places=1)
        self.assertAlmostEqual(self.e.con.master_db, -4.0, places=1)

    def test_command_dispatch(self):
        self.e.command({"type": "fader", "ch": 4, "db": -6.0})
        self.assertAlmostEqual(self.e.assistant.fader_db[4], -6.0, places=1)
        self.e.command({"type": "mute", "ch": 4, "on": True})
        self.assertTrue(self.e.muted[4])
        self.e.command({"type": "master", "db": -2.0})
        self.assertAlmostEqual(self.e.master_db, -2.0, places=1)

    def test_telemetry_has_mute_and_master(self):
        self.e.set_mute(5, True)
        snap = self.e.snapshot()
        ch5 = next(c for c in snap["channels"] if c["ch"] == 5)
        self.assertTrue(ch5["muted"])
        self.assertIn("master", snap)


class TestManualHold(unittest.TestCase):
    def test_human_move_makes_ride_yield_then_resume(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.enabled["vocal_ride"] = True
        a.lead_target = -6.0
        # human just touched the lead fader -> auto should yield for 5 s
        a.note_manual(C.LEAD_VOCAL_CH, now=0.0, hold=5.0)
        start = a.fader_db[C.LEAD_VOCAL_CH]
        for k in range(6):
            a.on_features(C.LEAD_VOCAL_CH, feat(rms=-30.0), now=0.5 + k * 0.3)
        self.assertEqual(a.fader_db[C.LEAD_VOCAL_CH], start)      # held -> no move
        # after the hold window, the ride resumes
        for k in range(10):
            a.on_features(C.LEAD_VOCAL_CH, feat(rms=-30.0), now=6.0 + k * 0.3)
        self.assertGreater(a.fader_db[C.LEAD_VOCAL_CH], start)


if __name__ == "__main__":
    unittest.main()
