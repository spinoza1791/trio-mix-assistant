import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.assistant import MixAssistant
from trio_mix.dsp import ChannelFeatures
from trio_mix.engine import Engine


class TestRoomConfidence(unittest.TestCase):
    def test_room_conf_quiet_vs_loud(self):
        self.assertEqual(MixAssistant._room_conf(-60.0, 30.0), 1.0)   # quiet + tonal
        self.assertLess(MixAssistant._room_conf(-10.0, 2.0), 0.5)     # loud + flat
        self.assertGreater(MixAssistant._room_conf(-10.0, 2.0), 0.0)

    def test_loud_room_lowers_confidence_and_warns(self):
        e = Engine(sim=True)
        a = e.assistant
        loud_flat = ChannelFeatures(rms_dbfs=-10.0, contrast_db=2.0)
        for _ in range(60):
            a._update_room(loud_flat, 0.0)
        self.assertLess(a.room_confidence, 0.6)
        self.assertTrue(any("SNR low" in ev["msg"] for ev in list(e.events)))

    def test_quiet_room_full_confidence(self):
        e = Engine(sim=True)
        a = e.assistant
        quiet = ChannelFeatures(rms_dbfs=-60.0, contrast_db=25.0)
        for _ in range(30):
            a._update_room(quiet, 0.0)
        self.assertGreaterEqual(a.room_confidence, 0.9)

    def test_low_confidence_raises_feedback_threshold(self):
        # with low confidence, the meas-mic feedback path needs more sustain
        e = Engine(sim=True)
        a = e.assistant
        a.room_confidence = 0.4
        a.enabled["feedback"] = True
        # a single rising-ring block must NOT immediately notch with low conf
        feat = ChannelFeatures(rms_dbfs=-20.0, fb_freq=2500.0)
        a.fb_last_freq[C.MEAS_MIC_CH] = 2500.0
        a.fb_last_level[C.MEAS_MIC_CH] = -40.0
        before = e.con.predip_freqs()
        a.handle_feedback(C.MEAS_MIC_CH, feat)       # one block only
        self.assertEqual(e.con.predip_freqs(), before)   # not notched yet

    def test_telemetry_room_field(self):
        e = Engine(sim=True)
        self.assertIn("room", e.telemetry)
        for k in ("level_db", "contrast_db", "confidence"):
            self.assertIn(k, e.telemetry["room"])


if __name__ == "__main__":
    unittest.main()
