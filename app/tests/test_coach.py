"""Layer-1 deterministic coach (advisory mode): the assistant computes the same
corrections as auto mode but, instead of writing to the console, recommends the
manual move. No LLM/AI. These tests pin that:
  * coach mode NEVER actuates the console, and
  * it produces the right recommendation with the exact numbers, and
  * auto mode is unchanged when coach is off.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.assistant import MixAssistant
from trio_mix.dsp import ChannelFeatures
from trio_mix.engine import Engine
from trio_mix.osc import SimConsole


def feat(rms=-30.0, peak=-20.0, fb=None, contrast=None):
    if contrast is None:
        contrast = 24.0 if fb else 6.0
    return ChannelFeatures(rms_dbfs=rms, peak_dbfs=peak, fb_freq=fb, contrast_db=contrast)


class TestCoachFeedback(unittest.TestCase):
    def test_channel_feedback_advises_instead_of_notching(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        for i in range(8):                                   # rising, stable ring
            a.on_features(2, feat(rms=-30 + i, fb=2500), now=0.0)
        self.assertFalse(con.notches.get(2))                 # console UNtouched
        rec = a.coach_recs.get(("feedback", 2))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["freq"], 2500)
        self.assertIn("notch input EQ band", rec["text"])
        # the instruction is also in the decision log as a 'coach' entry
        self.assertTrue(any(ev["kind"] == "coach" for ev in a.log))

    def test_room_feedback_advises_main_bus(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        for i in range(10):
            a.on_features(C.MEAS_MIC_CH, feat(rms=-30 + i, fb=2500, contrast=24.0), now=0.0)
        self.assertEqual(len(con.bus_eq), 0)                 # no bus EQ written
        rec = a.coach_recs.get(("feedback", C.MEAS_MIC_CH))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["target"], "main-bus")
        self.assertIn("MAIN BUS", rec["text"])

    def test_auto_mode_still_notches_when_coach_off(self):
        con = SimConsole(); a = MixAssistant(con)                 # coach OFF (default)
        for i in range(8):
            a.on_features(2, feat(rms=-30 + i, fb=2500), now=0.0)
        self.assertTrue(con.notches.get(2))                  # actuates as before
        self.assertEqual(a.coach_recs, {})


class TestCoachClip(unittest.TestCase):
    def test_clip_advises_no_gain_change(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        before = a.gain_db[3]
        a.on_features(3, feat(peak=-0.5), now=1.0)           # within 1 dB of FS
        self.assertEqual(a.gain_db[3], before)               # preamp untouched
        rec = a.coach_recs.get(("clip", 3))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["trim_db"], C.CLIP_TRIM_DB)
        self.assertIn("trim", rec["text"].lower())


class TestCoachRide(unittest.TestCase):
    def test_vocal_ride_advises_no_fader_change(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        a.enabled["vocal_ride"] = True
        a.lead_target = -6.0
        start = a.fader_db[C.LEAD_VOCAL_CH]
        now = 0.0
        for _ in range(10):
            a.on_features(C.LEAD_VOCAL_CH, feat(rms=-30.0), now=now)   # well below target
            now += 0.3
        self.assertEqual(a.fader_db[C.LEAD_VOCAL_CH], start)  # fader untouched
        rec = a.coach_recs.get(("vocal", C.LEAD_VOCAL_CH))
        self.assertIsNotNone(rec)
        self.assertGreater(rec["delta_db"], 0)               # advises moving it UP
        self.assertIn("move the fader up", rec["text"])


class TestCoachStateAndSnapshot(unittest.TestCase):
    def test_toggle_off_clears_recommendations(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        a.on_features(3, feat(peak=-0.5), now=1.0)
        self.assertTrue(a.coach_recs)
        a.set_coach_mode(False)
        self.assertEqual(a.coach_recs, {})
        self.assertEqual(a.coach_snapshot(), [])

    def test_snapshot_expires_stale_recs(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        a.on_features(3, feat(peak=-0.5), now=1.0)
        rec = a.coach_recs[("clip", 3)]
        # still fresh right now
        self.assertEqual(len(a.coach_snapshot(now=rec["mono"] + 1.0)), 1)
        # past the TTL -> dropped AND purged from state
        self.assertEqual(a.coach_snapshot(now=rec["mono"] + C.COACH_TTL_S + 1.0), [])
        self.assertNotIn(("clip", 3), a.coach_recs)

    def test_snapshot_freshest_first(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        a.on_features(3, feat(peak=-0.5), now=1.0)           # clip rec (seq 1)
        for i in range(8):
            a.on_features(2, feat(rms=-30 + i, fb=2500), now=0.0)  # feedback rec (later seq)
        snap = a.coach_snapshot()
        self.assertEqual(snap[0]["kind"], "feedback")        # most recent first
        self.assertEqual({r["kind"] for r in snap}, {"clip", "feedback"})


class TestCoachEngineWiring(unittest.TestCase):
    def test_command_toggles_coach_and_telemetry_reports_it(self):
        e = Engine(sim=True)
        e.command({"type": "coach", "on": True})
        self.assertTrue(e.assistant.coach_mode)
        self.assertTrue(e.telemetry["coach"]["on"])
        self.assertEqual(e.telemetry["op_mode"], "coach")
        self.assertIn("recs", e.telemetry["coach"])
        e.command({"type": "coach", "on": False})
        self.assertFalse(e.assistant.coach_mode)
        self.assertFalse(e.telemetry["coach"]["on"])

    def test_coach_snapshot_in_telemetry(self):
        e = Engine(sim=True)
        e.set_coach_mode(True)
        a = e.assistant
        for i in range(8):
            a.on_features(2, feat(rms=-30 + i, fb=2500), now=0.0)
        with e._lock:
            e._rebuild_telemetry()
        recs = e.telemetry["coach"]["recs"]
        self.assertTrue(any(r["kind"] == "feedback" and r["ch"] == 2 for r in recs))


if __name__ == "__main__":
    unittest.main()
