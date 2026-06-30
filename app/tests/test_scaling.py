import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import osc
from trio_mix import config as C


class TestScaling(unittest.TestCase):
    def test_fader_roundtrip(self):
        for db in [-90, -60, -45, -30, -20, -10, -5, 0, 5, 10]:
            f = osc.db_to_fader(db)
            self.assertGreaterEqual(f, 0.0)
            self.assertLessEqual(f, 1.0001)
            self.assertAlmostEqual(osc.fader_to_db(f), db, places=4)

    def test_fader_known_points(self):
        self.assertAlmostEqual(osc.db_to_fader(0.0), 0.75, places=3)   # 0.75 = 0 dB
        self.assertEqual(osc.db_to_fader(-95), 0.0)

    def test_freq_param_range(self):
        for hz in [20, 100, 250, 1000, 2500, 20000]:
            p = osc.freq_to_eq_param(hz)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0001)
            self.assertAlmostEqual(osc.eq_param_to_freq(p), hz, places=2)

    def test_q_param_range(self):
        for q in [0.3, 1, 4, 8, 10]:
            p = osc.q_to_param(q)
            self.assertGreaterEqual(p, -0.001)
            self.assertLessEqual(p, 1.001)

    def test_guardrail_clamp(self):
        con = osc.SimConsole()
        self.assertEqual(con.set_fader_db(1, +99), C.FADER_MAX_DB)
        self.assertEqual(con.set_fader_db(1, -99), C.FADER_MIN_DB)
        # headamp clamp
        g = con.nudge_gain_db(1, 60.0, +50.0)
        self.assertLessEqual(g, C.HEADAMP_MAX_DB)


if __name__ == "__main__":
    unittest.main()
