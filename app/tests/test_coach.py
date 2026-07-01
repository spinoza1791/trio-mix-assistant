"""Layer-1 deterministic coach (advisory mode): the assistant computes the same
corrections as auto mode but, instead of writing to the console, recommends the
manual move. No LLM/AI. These tests pin that:
  * coach mode NEVER actuates the console, and
  * it produces the right recommendation with the exact numbers, and
  * auto mode is unchanged when coach is off.
"""
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

    def test_clip_recovery_advises_restore_not_actuate(self):
        # A channel trimmed in auto, then switched to coach: when it's clean again
        # the app must ADVISE restoring the preamp, never creep the gain itself.
        con = SimConsole(); a = MixAssistant(con)
        a.gain_db[3] = a.nominal_gain[3] - 4.0            # left trimmed from auto mode
        a.last_clip_t[3] = -1e9                           # clip was long ago
        a.set_coach_mode(True)
        before = a.gain_db[3]
        a.on_features(3, feat(peak=-30.0), now=1e6)       # clean, well past recover window
        self.assertEqual(a.gain_db[3], before)            # preamp untouched
        rec = a.coach_recs.get(("clip", 3))
        self.assertIsNotNone(rec)
        self.assertIn("restore", rec["text"].lower())


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

    def test_balance_advises_no_fader_change(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        a.enabled["balance"] = True
        bch = C.BALANCE_CHANNELS[0]
        a.balance_targets = {bch: -10.0}                     # held output target
        start = a.fader_db[bch]
        now = 0.0
        for _ in range(6):
            a.on_features(bch, feat(rms=-28.0), now=now)     # 18 dB under target -> wants up
            now += 1.1
        self.assertEqual(a.fader_db[bch], start)             # fader untouched
        rec = a.coach_recs.get(("balance", bch))
        self.assertIsNotNone(rec)
        self.assertIn("move the fader", rec["text"])


class TestCoachScope(unittest.TestCase):
    """Coach governs the assistant's automatic MIX corrections. The operator's
    manual surface and the setlist show-automation are a different axis and must
    keep working when coach is on."""

    def test_manual_fader_still_actuates_under_coach(self):
        e = Engine(sim=True)
        e.set_coach_mode(True)
        e.set_fader(1, -8.0)                                 # operator move via the surface
        self.assertAlmostEqual(e.assistant.fader_db[1], -8.0, places=1)
        self.assertEqual(e.assistant.coach_recs, {})         # a manual move isn't a coach rec

    def test_scene_recall_still_actuates_under_coach(self):
        # Show-sheet automation (scene recall) is not a coached mix move, so a
        # manual/auto recall still fires in coach mode.
        e = Engine(sim=True)
        e.set_coach_mode(True)
        e.recall_scene_manual(2)
        self.assertEqual(getattr(e.con, "last_scene", None), 2)


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


class TestCoachCalibration(unittest.TestCase):
    def test_plan_matches_apply_bands(self):
        # The plan() the coach advises is the exact same bands apply() writes.
        con = SimConsole(); cal = Calibrator(con)
        res = cal.analyze(sim_room_capture())
        steps = cal.plan(res)
        self.assertTrue(steps)
        used = cal.apply(res, log=lambda *_: None)
        self.assertEqual({s["band"] for s in steps}, used)
        for s in steps:                                     # every advised step was written
            self.assertAlmostEqual(con.bus_eq[s["band"]]["hz"], s["hz"], places=1)

    def test_coach_calibration_recommends_main_bus_peq(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        cal = Calibrator(con)
        a.coach_calibration(cal.plan(cal.analyze(sim_room_capture())))
        rec = a.coach_recs.get(("calibration", None))
        self.assertIsNotNone(rec)
        self.assertIn("MAIN-BUS PEQ", rec["text"])
        self.assertTrue(rec["persist"])
        self.assertTrue(rec["steps"])

    def test_calibration_rec_does_not_expire(self):
        con = SimConsole(); a = MixAssistant(con)
        a.set_coach_mode(True)
        a.coach_calibration([{"band": 1, "hz": 250.0, "gain": -6.0, "q": 3.0, "kind": "room"}])
        rec = a.coach_recs[("calibration", None)]
        # long past the live-rec TTL, the persistent calibration advice survives
        snap = a.coach_snapshot(now=rec["mono"] + C.COACH_TTL_S + 100.0)
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["kind"], "calibration")

    def test_engine_coach_calibration_advises_not_applies(self):
        e = Engine(sim=True)
        e.set_coach_mode(True)
        e.run_calibration()                                 # spawns a worker (sleeps ~1.2 s)
        deadline = time.time() + 5.0
        while e.calib_status != "done" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(e.calib_status, "done")
        self.assertEqual(len(e.con.bus_eq), 0)              # console NOT written in coach mode
        self.assertTrue(any(r["kind"] == "calibration" for r in e.assistant.coach_snapshot()))

    def test_engine_auto_calibration_still_applies(self):
        e = Engine(sim=True)                                # coach OFF (default)
        e.run_calibration()
        deadline = time.time() + 5.0
        while e.calib_status != "done" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(e.calib_status, "done")
        self.assertGreater(len(e.con.bus_eq), 0)            # bus EQ written as before
        self.assertEqual(e.assistant.coach_recs, {})


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
