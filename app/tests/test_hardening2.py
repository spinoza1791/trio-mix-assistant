import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.assistant import MixAssistant
from trio_mix.calibration import Calibrator
from trio_mix.dsp import ChannelFeatures
from trio_mix.engine import Engine, sim_room_capture
from trio_mix.osc import SimConsole


def feat(rms=-30.0, peak=-20.0, fb=None, contrast=None):
    if contrast is None:
        contrast = 24.0 if fb else 6.0     # a real ring is high-contrast (peak >> floor)
    return ChannelFeatures(rms_dbfs=rms, peak_dbfs=peak, fb_freq=fb, contrast_db=contrast)


class TestCalibrationSplit(unittest.TestCase):
    def test_analyze_is_pure_apply_claims_bands(self):
        con = SimConsole()
        cal = Calibrator(con)
        res = cal.analyze(sim_room_capture())
        self.assertEqual(len(con.bus_eq), 0)          # analyze touches no console state
        self.assertTrue(res.corrections)
        used = cal.apply(res, log=lambda *_: None)
        self.assertTrue(used)                          # claimed >= 1 main-bus band
        self.assertTrue(used.issubset(set(con.bus_eq)))
        for b in used:                                 # claimed bands carry a real cut
            self.assertLess(con.bus_eq[b]["gain_db"], 0)
        for b in set(con.bus_eq) - used:               # the rest are flattened, not stale
            self.assertEqual(con.bus_eq[b]["gain_db"], 0)


class TestBusBandAllocator(unittest.TestCase):
    def _drive_room_feedback(self, a):
        for i in range(8):
            a.on_features(C.MEAS_MIC_CH, feat(rms=-30 + i, fb=2500.0), now=0.0)

    def test_feedback_never_clobbers_calibration_bands(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.cal_bus_bands = {1, 2, 5, 6}                  # calibration owns these
        self._drive_room_feedback(a)
        self.assertTrue(a.fb_bus_bands)                 # a notch was parked
        for b in a.fb_bus_bands:
            self.assertNotIn(b, a.cal_bus_bands)

    def test_reset_preserves_calibration_cuts(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.cal_bus_bands = {1, 2}
        con.set_bus_eq(1, 250.0, -6.0)                 # a calibration cut on band 1
        self._drive_room_feedback(a)
        self.assertTrue(a.fb_bus_bands)
        a.reset_notches()
        self.assertEqual(a.fb_bus_bands, [])
        self.assertLess(con.bus_eq[1]["gain_db"], 0)   # calibration cut still there


class TestRecalibrationResetsBus(unittest.TestCase):
    def test_calibration_flattens_bus_and_resets_feedback_tracking(self):
        e = Engine(sim=True)
        a = e.assistant
        a.fb_bus_bands = [3]                # pretend feedback parked a notch
        a.used_bus_notch_band = 5
        e.con.set_bus_eq(3, 2500.0, -9.0)
        e.run_calibration()
        time.sleep(1.7)                     # worker: 1.2 s + analyze + apply
        self.assertEqual(e.calib_status, "done")
        self.assertEqual(a.fb_bus_bands, [])      # feedback tracking reset
        self.assertEqual(a.used_bus_notch_band, 0)
        self.assertEqual(a.cal_bus_bands, {1, 2}) # sim corrections -> bands 1,2
        self.assertEqual(e.con.bus_eq[3]["gain_db"], 0)  # old feedback band flattened


class TestHardwareCalibrationGuard(unittest.TestCase):
    def test_calibration_aborts_cleanly_without_audio_backend(self):
        from trio_mix.osc import OscConsole
        con = OscConsole("127.0.0.1")                  # no packets sent on the abort path
        e = Engine(console=con, sim=False)
        self.assertIsNone(e.source)
        e.run_calibration()
        time.sleep(1.6)                                # worker: 1.2 s + abort
        self.assertEqual(e.calib_status, "none")
        self.assertEqual(e.status, "live")             # not stuck "calibrating"
        e.stop()


if __name__ == "__main__":
    unittest.main()
