import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.engine import Engine
from trio_mix.sessionlog import SessionLog
from trio_mix.venue import VenueModel, build_model, load_model, save_model, slug


class TestVenueModel(unittest.TestCase):
    def test_build_model_buckets_freqs_and_counts_shows(self):
        sl = SessionLog(":memory:")
        try:
            sl.start_session(venue="Barn")
            for msg in ("2500 Hz on lead_vox → notch", "2480 Hz on lead_vox → notch",
                        "2510 Hz on lead_vox → notch", "180 Hz on bass → notch"):
                sl.log_event({"kind": "feedback", "msg": msg})
            sl.flush()
            sl.start_session(venue="Barn")              # a second show
            sl.log_event({"kind": "feedback", "msg": "2495 Hz on lead → notch"})
            sl.flush()
            m = build_model(sl, "Barn")
            self.assertEqual(m.shows, 2)
            self.assertGreater(m.confidence, 0.0)
            top = m.feedback_freqs[0]
            self.assertTrue(2400 <= top["hz"] <= 2600)      # the 2.5 kHz bin dominates
            self.assertGreaterEqual(top["count"], 4)
            self.assertTrue(any(120 <= f["hz"] <= 220 for f in m.feedback_freqs))  # 180 Hz too
        finally:
            sl.close()

    def test_watch_freqs_respects_min_count(self):
        m = VenueModel(feedback_freqs=[{"hz": 2500, "count": 4}, {"hz": 180, "count": 1}])
        self.assertEqual(m.watch_freqs(min_count=2), [2500.0])

    def test_save_load_roundtrip(self):
        d = tempfile.mkdtemp()
        try:
            m = VenueModel(venue="The Barn", shows=3,
                           feedback_freqs=[{"hz": 2500, "count": 5}], confidence=1.0)
            path = save_model(m, d)
            self.assertTrue(os.path.exists(path))
            m2 = load_model("The Barn", d)
            self.assertEqual(m2.shows, 3)
            self.assertEqual(m2.feedback_freqs[0]["hz"], 2500)
            self.assertIsNone(load_model("Unknown Place", d))
        finally:
            shutil.rmtree(d)

    def test_slug(self):
        self.assertEqual(slug("The Cellar!"), "the-cellar")
        self.assertEqual(slug(""), "venue")


class TestEngineVenueIntegration(unittest.TestCase):
    def test_apply_seeds_watchlist_and_telemetry(self):
        e = Engine(sim=True)
        m = VenueModel(venue="Barn", shows=3,
                       feedback_freqs=[{"hz": 2500, "count": 4}], confidence=1.0)
        e.apply_venue_model(m)
        self.assertIn(2500.0, e.assistant.venue_watch_freqs)
        self.assertTrue(e.assistant._is_near_watch(2500.0))   # seeded -> reacts sooner
        self.assertEqual(e.telemetry["venue"]["shows"], 3)
        self.assertIn(2500, e.telemetry["venue"]["freqs"])

    def test_venue_watch_survives_recalibration(self):
        e = Engine(sim=True)
        e.apply_venue_model(VenueModel(feedback_freqs=[{"hz": 2500, "count": 4}]))
        e.assistant.set_calibration(None)         # recalibrate wipes calib watch list
        self.assertTrue(e.assistant._is_near_watch(2500.0))   # venue prior persists

    def test_learn_venue_writes_model(self):
        d = tempfile.mkdtemp()
        sl = SessionLog(":memory:")
        sl.start_session(venue="Cellar")
        e = Engine(sim=True)
        e.session_log = sl
        e.venue, e.venue_dir = "Cellar", d
        try:
            e.assistant._emit("feedback", "2500 Hz on lead → notch", 1)  # -> session log
            path = e.learn_venue()
            self.assertIsNotNone(path)
            m = load_model("Cellar", d)
            self.assertEqual(m.shows, 1)
            self.assertTrue(any(2400 <= f["hz"] <= 2600 for f in m.feedback_freqs))
        finally:
            sl.close()
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
