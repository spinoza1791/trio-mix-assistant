import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.engine import Engine
from trio_mix.showclock import ShowState


class TestLatencyInstrumentation(unittest.TestCase):
    def test_perf_present_and_populated(self):
        e = Engine(sim=True, tick=0.01)
        self.assertIn("perf", e.telemetry)
        for k in ("tick_ms", "jitter_ms", "max_ms"):
            self.assertIn(k, e.telemetry["perf"])
        e.start()
        try:
            time.sleep(0.3)                       # let the loop run a few ticks
            perf = e.telemetry["perf"]
            self.assertGreater(perf["tick_ms"], 0.0)      # actually measured compute time
            self.assertLess(perf["tick_ms"], 100.0)       # sim tick is cheap
        finally:
            e.stop()


class TestGuestMode(unittest.TestCase):
    def test_guest_song_unmutes_guest_channels(self):
        with mock.patch.object(C, "GUEST_CHANNELS", (7,)):
            e = Engine(sim=True)
            # a guest song unmutes the guest channel
            e.on_song_change(ShowState(song_index=3, song_name="Tide", playing=True))
            self.assertFalse(e.muted[7])
            # a non-guest song mutes it again
            e.on_song_change(ShowState(song_index=0, song_name="Gravity", playing=True))
            self.assertTrue(e.muted[7])

    def test_default_no_guest_channels_is_noop(self):
        e = Engine(sim=True)                       # GUEST_CHANNELS empty by default
        before = dict(e.muted)
        e.on_song_change(ShowState(song_index=3, song_name="Tide", playing=True))
        self.assertEqual(e.muted, before)          # nothing muted/unmuted

    def test_guest_mute_yields_to_operator(self):
        with mock.patch.object(C, "GUEST_CHANNELS", (7,)):
            e = Engine(sim=True)
            e.set_mute(7, True)                    # operator mutes the guest channel
            # a guest song would normally unmute it, but the op just touched it
            e.on_song_change(ShowState(song_index=3, song_name="Tide", playing=True))
            self.assertTrue(e.muted[7])            # operator's mute preserved


class _Feat:
    def __init__(self, rms):
        self.rms_dbfs = rms
        self.peak_dbfs = rms
        self.fb_freq = None


class TestStageMic(unittest.TestCase):
    def test_sustained_rise_warns(self):
        with mock.patch.object(C, "STAGE_MIC_CH", 9), mock.patch.object(C, "STAGE_RISE_DB", 4.0):
            e = Engine(sim=True)
            a = e.assistant
            for _ in range(200):                          # settle a quiet baseline
                a.handle_stage_volume(_Feat(-50.0), 0.0)
            a.last_stage_warn = -1e9
            a.handle_stage_volume(_Feat(-30.0), 100.0)    # sudden +20 dB on stage
            self.assertTrue(any(ev["kind"] == "system" and "stage volume up" in ev["msg"]
                                for ev in list(e.events)))

    def test_default_off(self):
        self.assertIsNone(C.STAGE_MIC_CH)                 # disabled unless configured


class TestStereoLinks(unittest.TestCase):
    def test_linked_fader_mirrors(self):
        with mock.patch.object(C, "STEREO_LINKS", ((2, 3),)):
            e = Engine(sim=True)
            e.set_fader(2, -5.0)
            self.assertAlmostEqual(e.assistant.fader_db[2], -5.0, places=1)
            self.assertAlmostEqual(e.assistant.fader_db[3], -5.0, places=1)  # partner followed

    def test_partner_lookup(self):
        with mock.patch.object(C, "STEREO_LINKS", ((2, 3),)):
            self.assertEqual(Engine._linked_partner(2), 3)
            self.assertEqual(Engine._linked_partner(3), 2)
            self.assertIsNone(Engine._linked_partner(5))


if __name__ == "__main__":
    unittest.main()
