"""Vocal-unmask ducking: dynamic mid-high EQ dip sidechained to the lead vocal.

- Auto mode: vocal present -> the reserved band on each instrument dips toward
  UNMASK_DEPTH_DB (fast attack); vocal gone -> it releases toward 0 (slow).
- Coach mode: advises a static dip instead of live ducking; console untouched.
- Disable / coach / takeover releases the band.
"""
import os
import sys
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.assistant import MixAssistant
from trio_mix.dsp import ChannelFeatures
from trio_mix.engine import Engine
from trio_mix.osc import SimConsole


def vox(rms):
    return ChannelFeatures(rms_dbfs=rms, peak_dbfs=rms + 6)


class TestUnmaskAssistant(unittest.TestCase):
    def _a(self, coach=False):
        con = SimConsole(); a = MixAssistant(con)
        a.enabled["unmask"] = True
        if coach:
            a.set_coach_mode(True)
        return con, a

    def _band(self, con, ch):
        return con.eq_bands.get(ch, {}).get(C.UNMASK_BAND)

    def test_ducks_when_vocal_present(self):
        con, a = self._a()
        target = a._unmask_targets()[0]
        for _ in range(20):
            a.handle_unmask(vox(-15.0), now=0.0)           # loud vocal -> present
        self.assertLess(a.duck_gain, -2.0)                 # ducked a few dB
        b = self._band(con, target)
        self.assertIsNotNone(b)
        self.assertLess(b["gain_db"], -1.0)                # a real cut on the reserved band
        self.assertAlmostEqual(b["hz"], C.UNMASK_FREQ, delta=1)

    def test_releases_when_vocal_absent(self):
        con, a = self._a()
        for _ in range(20):
            a.handle_unmask(vox(-15.0), now=0.0)           # duck in
        ducked = a.duck_gain
        for _ in range(40):
            a.handle_unmask(vox(-80.0), now=0.0)           # silence -> release
        self.assertGreater(a.duck_gain, ducked)            # moved back toward 0
        self.assertGreater(a.duck_gain, ducked + 1.0)      # meaningfully released

    def test_attack_faster_than_release(self):
        # symmetry check: N blocks of duck-in should move further than N of release
        _, a1 = self._a()
        for _ in range(4):
            a1.handle_unmask(vox(-15.0), now=0.0)
        attack_travel = abs(a1.duck_gain - 0.0)
        _, a2 = self._a()
        a2.duck_gain = C.UNMASK_DEPTH_DB                    # start fully ducked
        for _ in range(4):
            a2.handle_unmask(vox(-80.0), now=0.0)
        release_travel = abs(a2.duck_gain - C.UNMASK_DEPTH_DB)
        self.assertGreater(attack_travel, release_travel)

    def test_coach_advises_static_dip_no_actuation(self):
        con, a = self._a(coach=True)
        a.handle_unmask(vox(-15.0), now=0.0)
        self.assertEqual(con.eq_bands, {})                 # console untouched in coach
        rec = a.coach_recs.get(("unmask", C.LEAD_VOCAL_CH))
        self.assertIsNotNone(rec)
        self.assertIn("unmask the vocal", rec["text"])
        self.assertEqual(rec["freq"], round(C.UNMASK_FREQ))

    def test_release_flattens_band(self):
        con, a = self._a()
        for _ in range(20):
            a.handle_unmask(vox(-15.0), now=0.0)
        self.assertLess(self._band(con, a._unmask_targets()[0])["gain_db"], -1.0)
        a.release_unmask()
        self.assertEqual(a.duck_gain, 0.0)
        self.assertEqual(a.duck_applied, {})
        self.assertEqual(self._band(con, a._unmask_targets()[0])["gain_db"], 0.0)  # flat

    def test_disabled_is_inert(self):
        con = SimConsole(); a = MixAssistant(con)           # unmask not enabled
        for _ in range(20):
            a.handle_unmask(vox(-15.0), now=0.0)
        self.assertEqual(con.eq_bands, {})

    def test_targets_exclude_vocal_and_meas(self):
        _, a = self._a()
        t = a._unmask_targets()
        self.assertNotIn(C.LEAD_VOCAL_CH, t)
        self.assertNotIn(C.MEAS_MIC_CH, t)
        self.assertIn(4, t)                                 # an instrument channel


class TestUnmaskEngine(unittest.TestCase):
    def test_disable_releases_and_telemetry(self):
        e = Engine(sim=True)
        e.set_enabled("unmask", True)
        a = e.assistant
        for _ in range(20):
            a.handle_unmask(vox(-15.0), now=0.0)
        self.assertLess(a.duck_gain, -1.0)
        e.set_enabled("unmask", False)                      # must flatten the duck
        self.assertEqual(a.duck_gain, 0.0)
        self.assertEqual(a.duck_applied, {})
        self.assertIn("unmask", e.telemetry)
        self.assertFalse(e.telemetry["unmask"]["enabled"])

    def test_takeover_releases_duck(self):
        e = Engine(sim=True)
        e.set_enabled("unmask", True)
        for _ in range(20):
            e.assistant.handle_unmask(vox(-15.0), now=0.0)
        self.assertLess(e.assistant.duck_gain, -1.0)
        e.panic(True)                                       # takeover
        self.assertEqual(e.assistant.duck_gain, 0.0)


if __name__ == "__main__":
    unittest.main()
