"""Phase / polarity job: cross-correlation detection + auto-flip + coach advice.

- DSP phase_relation: polarity (zero-lag sign) and comb (best-lag) measurement.
- Assistant handle_phase: AUTO polarity flip (guard-railed), COACH advice, comb
  advisory, and the 'armed' anti-oscillation guard.
- Engine: a correlated, inverted pair gets its polarity flipped on the console.
"""
import os
import sys
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix import dsp
from trio_mix.assistant import MixAssistant
from trio_mix.capture import CaptureSource
from trio_mix.engine import Engine
from trio_mix.osc import SimConsole


def _rel(zero, best=None, lag_ms=0.0):
    return dsp.PhaseRelation(zero_corr=zero, best_corr=(zero if best is None else best),
                             lag_ms=lag_ms)


class TestPhaseDSP(unittest.TestCase):
    def _sig(self, n=1024, f=300.0):
        t = np.arange(n) / C.SAMPLE_RATE
        return (np.sin(2 * np.pi * f * t) + 0.03 * np.sin(2 * np.pi * 900 * t)).astype(np.float64)

    def test_polarity_sign(self):
        a = self._sig()
        self.assertGreater(dsp.phase_relation(a, a.copy()).zero_corr, 0.95)     # in phase
        self.assertLess(dsp.phase_relation(a, -a).zero_corr, -0.95)             # inverted

    def test_comb_lag_detected(self):
        a = self._sig()
        b = np.roll(a, 24)                                   # 24 samples = 0.5 ms
        rel = dsp.phase_relation(a, b)
        self.assertGreater(abs(rel.best_corr), 0.9)          # still highly correlated
        self.assertAlmostEqual(abs(rel.lag_ms), 24 / C.SAMPLE_RATE * 1000, delta=0.1)

    def test_uncorrelated_and_silence(self):
        a = self._sig()
        self.assertLess(abs(dsp.phase_relation(a, self._sig(f=737.0)
                                               + np.random.randn(a.size)).zero_corr), 0.5)
        self.assertEqual(dsp.phase_relation(np.zeros(1024), a).zero_corr, 0.0)


class TestPhaseAssistant(unittest.TestCase):
    def _a(self, coach=False):
        con = SimConsole()
        a = MixAssistant(con)
        a.enabled["phase"] = True
        if coach:
            a.set_coach_mode(True)
        return con, a

    def test_auto_flip_on_sustained_inversion(self):
        con, a = self._a()
        for _ in range(C.PHASE_SUSTAIN + 1):
            a.handle_phase(4, 5, _rel(-0.9), now=0.0)
        self.assertTrue(con.polarity.get(5))                 # console polarity flipped
        self.assertTrue(a.polarity[5])                       # believed state updated
        flips = [w for w in con.wire_log if w[0] == "/ch/05/preamp/invert"]
        self.assertEqual(len(flips), 1)                      # exactly one flip (armed guard)

    def test_no_flip_before_sustain(self):
        con, a = self._a()
        for _ in range(C.PHASE_SUSTAIN - 2):
            a.handle_phase(4, 5, _rel(-0.9), now=0.0)
        self.assertFalse(con.polarity.get(5))                # not yet

    def test_armed_guard_stops_oscillation(self):
        con, a = self._a()
        # drive well past sustain + cooldown; still only ONE flip (it didn't help)
        for i in range(60):
            a.handle_phase(4, 5, _rel(-0.9), now=i * 1.0)    # now advances past cooldown
        flips = [w for w in con.wire_log if w[0] == "/ch/05/preamp/invert"]
        self.assertEqual(len(flips), 1)

    def test_coach_advises_flip_no_actuation(self):
        con, a = self._a(coach=True)
        for _ in range(C.PHASE_SUSTAIN + 1):
            a.handle_phase(4, 5, _rel(-0.9), now=0.0)
        self.assertFalse(con.polarity.get(5))                # console untouched in coach
        rec = a.coach_recs.get(("phase", 4))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "inverted")
        self.assertIn("invert phase", rec["text"])

    def test_comb_is_advisory_never_flips(self):
        con, a = self._a()
        a.handle_phase(4, 5, _rel(0.85, best=0.95, lag_ms=1.2), now=0.0)
        self.assertFalse(con.polarity.get(5))                # comb isn't a polarity fix
        self.assertTrue(any(e["kind"] == "phase" and "comb filtering" in e["msg"]
                            for e in a.log))

    def test_aligned_and_unrelated_do_nothing(self):
        con, a = self._a()
        a.handle_phase(4, 5, _rel(0.9), now=0.0)             # in phase, aligned
        a.handle_phase(6, 7, _rel(0.1), now=0.0)             # unrelated
        self.assertEqual(con.polarity, {})
        self.assertEqual(a.coach_recs, {})

    def test_disabled_job_is_inert(self):
        con = SimConsole(); a = MixAssistant(con)            # phase not enabled
        for _ in range(20):
            a.handle_phase(4, 5, _rel(-0.9), now=0.0)
        self.assertEqual(con.polarity, {})


class _InvertedPairSource(CaptureSource):
    """Two channels carrying the SAME source with channel 5 polarity-inverted."""
    def __init__(self):
        self.n = C.BLOCK
        self.t0 = 0

    def block(self):
        t = (self.t0 + np.arange(self.n)) / C.SAMPLE_RATE
        self.t0 += self.n
        sig = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        return {4: sig, 5: (-sig).astype(np.float32),
                C.MEAS_MIC_CH: (0.01 * np.ones(self.n, np.float32))}


class TestPhaseEngine(unittest.TestCase):
    def test_engine_auto_flips_inverted_pair(self):
        e = Engine(sim=True, source=_InvertedPairSource(), tick=0.004)
        for k in e.assistant.enabled:
            e.assistant.enabled[k] = (k == "phase")
        with mock.patch.object(C, "PHASE_PAIRS", ((4, 5),)):
            e.start()
            deadline = time.time() + 3.0
            while time.time() < deadline and not e.con.polarity.get(5):
                time.sleep(0.02)
            e.stop()
        self.assertTrue(e.con.polarity.get(5))               # flipped the inverted channel
        tel = e.telemetry["phase"]
        self.assertTrue(tel["enabled"])
        self.assertTrue(any(p["status"] in ("inverted", "aligned") for p in tel["pairs"]))


class TestPhaseConfig(unittest.TestCase):
    def test_template_channel_map_sets_phase_pairs(self):
        saved = C.channel_map_state()
        try:
            C.apply_channel_map({"map": {"1": "gtr_mic", "2": "gtr_di"},
                                 "phase_pairs": [[1, 2]]})
            self.assertEqual(C.PHASE_PAIRS, ((1, 2),))
        finally:
            C.restore_channel_map_state(saved)
        self.assertEqual(C.PHASE_PAIRS, saved[-1])           # restored


if __name__ == "__main__":
    unittest.main()
