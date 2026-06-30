import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.engine import Engine


class TestPerformerEQ(unittest.TestCase):
    def test_set_eq_updates_state_console_telemetry(self):
        e = Engine(sim=True)
        e.set_eq(2, 1, hz=1000.0, gain=4.0, q=3.0, on=True)
        b = e.eq[2][1]
        self.assertEqual((b["hz"], b["gain"], b["q"], b["on"]), (1000.0, 4.0, 3.0, True))
        self.assertEqual(e.con.eq_bands[2][1]["gain_db"], 4.0)   # console got it
        tel = [x for x in e.telemetry["eq"][2] if x["band"] == 1][0]
        self.assertEqual(tel["gain"], 4.0)

    def test_eq_clamps(self):
        e = Engine(sim=True)
        e.set_eq(2, 1, hz=50000.0, gain=99.0, q=99.0, on=True)
        b = e.eq[2][1]
        self.assertEqual(b["hz"], 20000.0)      # clamped to 20k
        self.assertEqual(b["gain"], 15.0)       # clamped +15
        self.assertEqual(b["q"], 10.0)          # clamped 10

    def test_reset_channel_eq(self):
        e = Engine(sim=True)
        e.set_eq(4, 2, gain=6.0, on=True)
        e.reset_channel_eq(4)
        for b in e.eq[4].values():
            self.assertEqual(b["gain"], 0.0)
            self.assertFalse(b["on"])

    def test_reset_eq_resyncs_feedback_state(self):
        e = Engine(sim=True)
        e.assistant.used_notch_band[2] = 2          # detector believes a notch is parked
        e.assistant.fb_streak[2] = 3
        e.reset_channel_eq(2)                        # clears channel EQ (incl. the notch)
        self.assertEqual(e.assistant.used_notch_band[2], 0)   # resynced, not stuck
        self.assertEqual(e.assistant.fb_streak[2], 0)

    def test_meas_mic_excluded(self):
        e = Engine(sim=True)
        self.assertNotIn(C.MEAS_MIC_CH, e.eq)
        e.set_eq(C.MEAS_MIC_CH, 1, gain=5.0)    # ignored, no raise
        self.assertNotIn(C.MEAS_MIC_CH, e.eq)


class TestPerformerFX(unittest.TestCase):
    def test_set_send(self):
        e = Engine(sim=True)
        e.set_send(2, 1, -12.0)
        self.assertEqual(e.sends[2][1], -12.0)
        self.assertEqual(e.con.sends[(2, 1)], -12.0)
        self.assertEqual(e.telemetry["sends"][2][1], -12.0)

    def test_set_fx_wet(self):
        e = Engine(sim=True)
        e.set_fx_wet(1, -3.0)
        self.assertEqual(e.fx_wet[1], -3.0)
        self.assertEqual(e.con.fx_returns[1], -3.0)
        self.assertEqual(e.telemetry["fx"]["wet"]["1"], -3.0)

    def test_fx_buses_in_telemetry(self):
        e = Engine(sim=True)
        self.assertEqual(e.telemetry["fx"]["buses"]["1"], C.FX_BUSES[1])

    def test_invalid_fx_ignored(self):
        e = Engine(sim=True)
        e.set_send(2, 99, -10.0)                # no such FX bus -> ignored
        e.set_fx_wet(99, -10.0)
        self.assertNotIn(99, e.fx_wet)


class TestEQFXCommands(unittest.TestCase):
    def test_command_dispatch(self):
        e = Engine(sim=True)
        e.command({"type": "eq", "ch": 2, "band": 1, "gain": 3.0, "on": True})
        self.assertEqual(e.eq[2][1]["gain"], 3.0)
        e.command({"type": "send", "ch": 2, "fx": 1, "db": -8.0})
        self.assertEqual(e.sends[2][1], -8.0)
        e.command({"type": "fx", "fx": 2, "db": -5.0})
        self.assertEqual(e.fx_wet[2], -5.0)
        e.command({"type": "eq_reset", "ch": 2})
        self.assertEqual(e.eq[2][1]["gain"], 0.0)
        # malformed -> ignored, no raise
        e.command({"type": "eq", "ch": "x", "band": 1})
        e.command({"type": "send", "ch": 2, "fx": 1, "db": "loud"})


if __name__ == "__main__":
    unittest.main()
