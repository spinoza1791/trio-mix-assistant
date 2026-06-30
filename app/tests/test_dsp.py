import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import dsp
from trio_mix import config as C


class TestDSP(unittest.TestCase):
    def test_analyse_sine_levels(self):
        sr, n = C.SAMPLE_RATE, C.BLOCK
        t = np.arange(n) / sr
        x = 0.5 * np.sin(2 * np.pi * 1000 * t)
        f = dsp.analyse_block(x, sr)
        # rms of a 0.5 sine = 0.5/sqrt(2) -> ~ -9 dBFS; peak ~ -6 dBFS
        self.assertAlmostEqual(f.rms_dbfs, 20 * math.log10(0.5 / math.sqrt(2)), delta=0.5)
        self.assertAlmostEqual(f.peak_dbfs, 20 * math.log10(0.5), delta=0.5)

    def test_empty_block(self):
        f = dsp.analyse_block(np.zeros(0))
        self.assertEqual(f.rms_dbfs, -90.0)

    def test_detect_ring_finds_tone(self):
        sr, n = C.SAMPLE_RATE, C.BLOCK
        t = np.arange(n) / sr
        x = 0.4 * np.sin(2 * np.pi * 2500 * t) + 0.001 * np.random.randn(n)
        f = dsp.analyse_block(x, sr)
        self.assertIsNotNone(f.fb_freq)
        self.assertAlmostEqual(f.fb_freq, 2500, delta=60)

    def test_detect_ring_ignores_smooth_spectrum(self):
        # A flat spectrum has no peak-above-neighbours -> no ring.
        self.assertIsNone(dsp.detect_ring(np.ones(513)))
        # A smooth pink-like slope is likewise not a narrowband ring.
        slope = 1.0 / np.sqrt(np.linspace(1, 513, 513))
        self.assertIsNone(dsp.detect_ring(slope))
        # But a sharp narrowband spike IS flagged.
        spiky = np.ones(513)
        spiky[200] = 100.0
        self.assertIsNotNone(dsp.detect_ring(spiky))

    def test_pink_noise_is_smooth_no_false_peaks(self):
        # The calibration contract: a flat-ish pink spectrum must not produce
        # false resonant peaks (only a real room mode would). Seeded for
        # determinism.
        p = dsp.generate_pink_noise(2.0, rng=np.random.default_rng(0))
        self.assertEqual(p.size, int(2.0 * C.SAMPLE_RATE))
        corrections, watch = dsp.find_resonant_peaks(p)
        self.assertEqual(len(corrections), 0)
        self.assertEqual(len(watch), 0)

    def test_find_resonant_peaks(self):
        sr = C.SAMPLE_RATE
        noise = dsp.generate_pink_noise(2.0)
        t = np.arange(noise.size) / sr
        noise = noise + 0.30 * np.sin(2 * np.pi * 250 * t) + 0.22 * np.sin(2 * np.pi * 2000 * t)
        corrections, watch = dsp.find_resonant_peaks(noise, sr)
        watch_hz = [w for w, _ in watch]
        self.assertTrue(any(abs(h - 250) < 1 for h in watch_hz))
        self.assertTrue(any(abs(h - 2000) < 1 for h in watch_hz))
        # corrections are gentle (never below the cap)
        for _, cut in corrections:
            self.assertGreaterEqual(cut, C.CAL_MAX_CUT_DB)


if __name__ == "__main__":
    unittest.main()
