import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.assistant import MixAssistant
from trio_mix.dsp import ChannelFeatures
from trio_mix.osc import SimConsole


def feat(rms=-30.0, peak=-20.0, fb=None, contrast=None):
    if contrast is None:
        contrast = 24.0 if fb else 6.0     # a real ring is high-contrast (peak >> floor)
    return ChannelFeatures(rms_dbfs=rms, peak_dbfs=peak, fb_freq=fb, contrast_db=contrast)


class TestClip(unittest.TestCase):
    def test_clip_trims_preamp(self):
        con = SimConsole()
        a = MixAssistant(con)
        before = a.gain_db[3]
        a.on_features(3, feat(peak=-0.5), now=1.0)       # within 1 dB of FS
        self.assertLess(a.gain_db[3], before)
        self.assertEqual(a.gain_db[3], before + C.CLIP_TRIM_DB)

    def test_no_clip_when_safe(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.on_features(3, feat(peak=-12.0), now=1.0)
        self.assertEqual(a.gain_db[3], a.nominal_gain[3])

    def test_clip_recovers_when_clean(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.on_features(3, feat(peak=-0.5), now=1.0)        # trim
        trimmed = a.gain_db[3]
        # long after, clean signal -> creep back up
        a.on_features(3, feat(peak=-30.0), now=1.0 + C.CLIP_RECOVER_S + 1)
        self.assertGreater(a.gain_db[3], trimmed)


class TestFeedback(unittest.TestCase):
    def _drive(self, a, ch, freqs_levels):
        for f, lvl in freqs_levels:
            a.on_features(ch, feat(rms=lvl, peak=-20, fb=f), now=0.0)

    def test_rising_stable_ring_is_notched(self):
        con = SimConsole()
        a = MixAssistant(con)
        # stable 2500 Hz, rising level over several blocks
        self._drive(a, 2, [(2500, -30 + i) for i in range(8)])
        self.assertTrue(con.notches.get(2))
        self.assertAlmostEqual(con.notch_freqs(2)[0], 2500, delta=1)

    def test_steady_note_not_notched(self):
        con = SimConsole()
        a = MixAssistant(con)
        # stable freq but constant level (a held sung note) -> never rising
        self._drive(a, 1, [(220, -25) for _ in range(12)])
        self.assertFalse(con.notches.get(1))

    def test_wandering_freq_not_notched(self):
        con = SimConsole()
        a = MixAssistant(con)
        # rising level but the frequency drifts (vibrato) -> not stable
        self._drive(a, 1, [(220 + 30 * i, -25 + i) for i in range(10)])
        self.assertFalse(con.notches.get(1))

    def test_watchlist_reacts_sooner(self):
        # with 2500 on the watch-list, the notch needs one fewer block
        con_w = SimConsole(); aw = MixAssistant(con_w)
        aw.watch_freqs = [2500]
        con_n = SimConsole(); an = MixAssistant(con_n)
        seq = [(2500, -30 + i) for i in range(4)]   # short burst
        for f, lvl in seq:
            aw.on_features(2, feat(rms=lvl, fb=f), now=0.0)
            an.on_features(2, feat(rms=lvl, fb=f), now=0.0)
        # watch-listed catches within the short burst; the un-listed does not yet
        self.assertTrue(con_w.notches.get(2))
        self.assertFalse(con_n.notches.get(2))

    def test_reset_clears_notches(self):
        con = SimConsole()
        a = MixAssistant(con)
        self._drive(a, 2, [(2500, -30 + i) for i in range(8)])
        self.assertTrue(con.notches.get(2))
        a.reset_notches()
        self.assertFalse(con.notches.get(2))
        self.assertEqual(a.used_notch_band[2], 0)


class TestRides(unittest.TestCase):
    def test_vocal_ride_pushes_toward_target(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.enabled["vocal_ride"] = True
        a.lead_target = -6.0
        # input sits well below target -> fader should climb (and clamp at max)
        now = 0.0
        for _ in range(20):
            a.on_features(C.LEAD_VOCAL_CH, feat(rms=-30.0), now=now)
            now += 0.3
        self.assertGreater(a.fader_db[C.LEAD_VOCAL_CH], 0.0)
        self.assertLessEqual(a.fader_db[C.LEAD_VOCAL_CH], C.FADER_MAX_DB)

    def test_vocal_ride_respects_deadband(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.enabled["vocal_ride"] = True
        a.lead_target = -6.0
        # input already at target-ish (output_est ~ -6) -> no move
        now = 0.0
        for _ in range(10):
            a.on_features(C.LEAD_VOCAL_CH, feat(rms=-6.0), now=now)
            now += 0.3
        self.assertAlmostEqual(a.fader_db[C.LEAD_VOCAL_CH], 0.0, delta=1.01)

    def test_balance_capture_and_hold(self):
        con = SimConsole()
        a = MixAssistant(con)
        a.fader_db[5] = 0.0
        a.capture_balance({5: -20.0, 7: -18.0})
        self.assertAlmostEqual(a.balance_targets[5], -20.0, places=1)
        # enable + drive an off-target level -> a move happens
        a.enabled["balance"] = True
        now = 0.0
        start = a.fader_db[5]
        for _ in range(10):
            a.on_features(5, feat(rms=-28.0), now=now)   # 8 dB low
            now += 1.1
        self.assertNotEqual(a.fader_db[5], start)


if __name__ == "__main__":
    unittest.main()
