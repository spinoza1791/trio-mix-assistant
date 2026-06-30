import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.engine import Engine


class TestLatencyInstrumentation(unittest.TestCase):
    def test_aggregator(self):
        e = Engine(sim=True)
        e._note_latency_ev("feedback", 0.0012)      # 1.2 ms
        e._note_latency_ev("feedback", 0.0008)      # 0.8 ms
        lat = e._latency["feedback"]
        self.assertEqual(lat["n"], 2)
        self.assertGreater(lat["max"], 0.0)
        self.assertLess(lat["ema"], 2.0)
        self.assertIn("latency", e.telemetry)
        self.assertIn("block_ms", e.telemetry)

    def test_actuation_records_detect_to_actuate(self):
        # the sim closed loop catches feedback -> a feedback notch fires -> latency logged
        e = Engine(sim=True, tick=0.005)
        e.start()
        try:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not e.telemetry.get("latency"):
                time.sleep(0.05)
            self.assertTrue(e.telemetry["latency"], "no detect→actuate latency recorded")
            sample = next(iter(e.telemetry["latency"].values()))
            self.assertGreaterEqual(sample["last"], 0.0)
            self.assertLess(sample["last"], 200.0)    # in-process delta is tiny
        finally:
            e.stop()


if __name__ == "__main__":
    unittest.main()
