import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from trio_mix import config as C
from trio_mix import template as tmpl
from trio_mix.engine import Engine


class TestChannelMap(unittest.TestCase):
    def setUp(self):
        self._snap = C.channel_map_state()      # isolate: restore the global map after

    def tearDown(self):
        C.restore_channel_map_state(self._snap)

    def test_apply_mutates_config(self):
        C.apply_channel_map({"map": {"1": "v", "2": "g"}, "lead": 1, "meas_mic": 2,
                             "balance": [2], "guest": [2], "stereo_links": [[1, 2]],
                             "stage_mic": None})
        self.assertEqual(C.CHANNELS, {1: "v", 2: "g"})
        self.assertEqual(C.LEAD_VOCAL_CH, 1)
        self.assertEqual(C.MEAS_MIC_CH, 2)
        self.assertEqual(C.BALANCE_CHANNELS, (2,))
        self.assertEqual(C.STEREO_LINKS, ((1, 2),))

    def test_restore(self):
        C.apply_channel_map({"map": {"1": "v"}})
        C.restore_channel_map_state(self._snap)
        self.assertEqual(len(C.CHANNELS), 8)    # back to the trio default
        self.assertEqual(C.CHANNELS[1], "lead_vox")

    def test_autofoh_pilot_loads_and_applies(self):
        t = tmpl.load(os.path.join(APP, "templates", "autofoh_pilot.json"))
        self.assertIsNotNone(t.channels)
        C.apply_channel_map(t.channels)
        self.assertEqual(len(C.CHANNELS), 15)
        self.assertEqual(C.CHANNELS[1], "lead_vox")
        self.assertEqual(C.MEAS_MIC_CH, 14)
        self.assertEqual(C.STAGE_MIC_CH, 15)
        self.assertEqual(C.BALANCE_CHANNELS, (6, 7))
        self.assertIn((4, 5), C.STEREO_LINKS)
        self.assertIn(3, C.GUEST_CHANNELS)
        self.assertEqual(C.ROLE_LABELS["gtr_l"], "Guitar L")

    def test_engine_built_on_13ch_map(self):
        t = tmpl.load(os.path.join(APP, "templates", "autofoh_pilot.json"))
        C.apply_channel_map(t.channels)
        e = Engine(sim=True, template=t)
        self.assertIn(4, e.eq)                  # gtr_l has performer EQ
        self.assertNotIn(14, e.eq)              # meas mic excluded from EQ surface
        self.assertEqual(len(e.telemetry["channels"]), 15)
        e.set_fader(4, -5.0)                    # stereo link 4+5 mirrors
        self.assertAlmostEqual(e.assistant.fader_db[5], -5.0, places=1)

    def test_bench_mic_template(self):
        t = tmpl.load(os.path.join(APP, "templates", "bench_mic.json"))
        C.apply_channel_map(t.channels)
        self.assertEqual(C.CHANNELS, {1: "meas_mic"})
        self.assertEqual(C.MEAS_MIC_CH, 1)
        self.assertEqual(C.BALANCE_CHANNELS, ())        # cleared for the bench
        e = Engine(sim=True, template=t)
        self.assertEqual(len(e.telemetry["channels"]), 1)

    def test_template_balance_validated_against_own_map(self):
        with self.assertRaises(tmpl.TemplateError):
            tmpl.from_dict({"channels": {"map": {"1": "v"}},
                            "songs": [{"name": "A", "balance": {"9": -3}}]})
        t = tmpl.from_dict({"channels": {"map": {"1": "v", "2": "g"}},
                            "songs": [{"name": "A", "balance": {"2": -3}}]})
        self.assertEqual(t.songs[0].balance[2], -3.0)

    def test_apply_is_atomic_on_bad_spec(self):
        before = dict(C.CHANNELS)
        with self.assertRaises((ValueError, TypeError)):
            C.apply_channel_map({"map": {"1": "v"}, "lead": "notanint"})
        self.assertEqual(C.CHANNELS, before)        # not left half-applied

    def test_reapply_clears_stale_labels(self):
        C.apply_channel_map({"map": {"1": "alpha"}})
        C.apply_channel_map({"map": {"1": "beta"}})
        self.assertNotIn("alpha", C.ROLE_LABELS)    # stale label gone
        self.assertIn("beta", C.ROLE_LABELS)

    def test_invalid_channels_block_rejected(self):
        for bad in (
            {"channels": {"map": {"x": "v"}}},                       # non-int key
            {"channels": {"map": {"1": ""}}},                        # empty role
            {"channels": {"map": {"1": "v"}, "lead": 9}},            # lead not in map
            {"channels": {"map": {"1": "v"}, "stereo_links": [[1]]}},  # bad pair
        ):
            with self.assertRaises(tmpl.TemplateError):
                tmpl.from_dict(bad)


if __name__ == "__main__":
    unittest.main()
