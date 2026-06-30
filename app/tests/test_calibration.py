import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.calibration import Calibrator
from trio_mix.engine import sim_room_capture
from trio_mix.osc import SimConsole


class TestCalibration(unittest.TestCase):
    def test_run_applies_and_reports(self):
        con = SimConsole()
        cal = Calibrator(con)
        res = cal.run(lambda: sim_room_capture(), apply=True, log=lambda *_: None)

        # found the injected room boom (250) and hot resonance (~2000 octave)
        watch_hz = [w for w, _ in res.watchlist]
        self.assertTrue(any(abs(h - 250) < 1 for h in watch_hz))
        self.assertTrue(len(res.watchlist) >= 1)

        # applied at least one main-bus EQ band
        self.assertTrue(len(con.bus_eq) >= 1)

        # baseline covers the octave bands
        self.assertTrue(len(res.baseline) >= 8)

    def test_no_apply_leaves_console_untouched(self):
        con = SimConsole()
        cal = Calibrator(con)
        cal.run(lambda: sim_room_capture(), apply=False, log=lambda *_: None)
        self.assertEqual(len(con.bus_eq), 0)


if __name__ == "__main__":
    unittest.main()
