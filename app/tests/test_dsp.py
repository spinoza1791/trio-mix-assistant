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


class TestSubBinInterpolation(unittest.TestCase):
    def _mag(self, freq, n, sr=C.SAMPLE_RATE):
        t = np.arange(n) / sr
        x = 0.4 * np.sin(2 * np.pi * freq * t)
        return np.abs(np.fft.rfft(x * np.hanning(n)))

    def test_off_bin_tone_is_refined_below_bin_width(self):
        # A tone parked BETWEEN two FFT bins: raw bin-picking is off by up to
        # half a bin; parabolic interpolation must beat that.
        sr, n = C.SAMPLE_RATE, C.BLOCK
        bin_hz = sr / n                                  # ~46.9 Hz at 1024/48k
        freq = 305.0                                     # sits between bins
        est = dsp.detect_ring(self._mag(freq, n), sr)
        self.assertIsNotNone(est)
        self.assertLess(abs(est - freq), bin_hz / 4)     # well inside a bin

    def test_interpolation_beats_raw_bin(self):
        sr, n = C.SAMPLE_RATE, C.BLOCK
        freq = 181.0
        mag = self._mag(freq, n)
        raw_bin = int(np.argmax(20 * np.log10(mag + 1e-9)))
        raw_hz = raw_bin * (sr / 2) / (mag.size - 1)
        interp = dsp.detect_ring(mag, sr)
        self.assertLess(abs(interp - freq), abs(raw_hz - freq))

    def test_on_bin_tone_unchanged(self):
        # A tone exactly on a bin centre must not be pushed off it.
        sr, n = C.SAMPLE_RATE, C.BLOCK
        k = 40
        freq = k * (sr / 2) / (n // 2)                   # exact bin-centre freq
        est = dsp.detect_ring(self._mag(freq, n), sr)
        self.assertAlmostEqual(est, freq, delta=2.0)


class TestHarmonicity(unittest.TestCase):
    def _sig(self, comps, n=4096, sr=C.SAMPLE_RATE):
        t = np.arange(n) / sr
        x = sum(a * np.sin(2 * np.pi * f * t) for f, a in comps)
        return np.abs(np.fft.rfft(x * np.hanning(n)))

    def test_pure_tone_is_not_harmonic(self):
        mag = self._sig([(1500.0, 0.5)])                 # single partial only
        self.assertLess(dsp.ring_harmonicity(mag), 0.15)

    def test_musical_tone_is_harmonic(self):
        # fundamental + 2nd + 3rd harmonic = a musical note, not a ring
        mag = self._sig([(1500.0, 0.6), (3000.0, 0.4), (4500.0, 0.3)])
        self.assertGreater(dsp.ring_harmonicity(mag), 0.3)

    def test_no_ring_no_harmonicity(self):
        self.assertEqual(dsp.ring_harmonicity(np.ones(513)), 0.0)


class TestRollingRingDetector(unittest.TestCase):
    def test_high_res_refines_low_tone(self):
        # Stream 1024-sample blocks of a low off-bin tone; once the 4096 rolling
        # window fills, its resolution (~11.7 Hz/bin) beats the 1024 block.
        sr = C.SAMPLE_RATE
        freq = 137.0
        det = dsp.RollingRingDetector(sr=sr)
        t0 = 0
        est = None
        for _ in range(6):                               # > 4096/1024 blocks to fill
            t = (t0 + np.arange(C.BLOCK)) / sr
            t0 += C.BLOCK
            block = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
            est, _harm = det.update(block)
        self.assertIsNotNone(est)
        self.assertLess(abs(est - freq), 6.0)            # finer than a 1024 bin (~47 Hz)

    def test_reports_harmonicity(self):
        sr = C.SAMPLE_RATE
        det = dsp.RollingRingDetector(sr=sr)
        t0 = 0
        harm = 0.0
        for _ in range(6):
            t = (t0 + np.arange(C.BLOCK)) / sr
            t0 += C.BLOCK
            x = (0.6 * np.sin(2 * np.pi * 1500 * t)
                 + 0.4 * np.sin(2 * np.pi * 3000 * t)
                 + 0.3 * np.sin(2 * np.pi * 4500 * t)).astype(np.float32)
            _est, harm = det.update(x)
        self.assertGreater(harm, 0.3)


if __name__ == "__main__":
    unittest.main()
