import json
import math
import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import dsp
from trio_mix import osc
from trio_mix.engine import Engine


class TestInputHardening(unittest.TestCase):
    def test_command_tolerates_malformed_and_hostile_input(self):
        e = Engine(sim=True)
        before = dict(e.assistant.fader_db)
        for bad in [None, "x", 5, [], {}, {"type": "fader"},
                    {"type": "fader", "ch": "nope", "db": 0},
                    {"type": "fader", "ch": 1, "db": float("nan")},
                    {"type": "master", "db": float("inf")},
                    {"type": "lead_target", "db": "loud"},
                    {"type": "toggle"}, {"type": "unknown"}]:
            e.command(bad)                       # must never raise
        self.assertEqual(e.assistant.fader_db, before)   # nothing applied

    def test_setters_reject_non_finite(self):
        e = Engine(sim=True)
        e.set_fader(1, float("nan"))
        e.set_master(float("inf"))
        e.set_lead_target(float("-inf"))
        self.assertTrue(math.isfinite(e.assistant.fader_db[1]))
        self.assertTrue(math.isfinite(e.master_db))
        self.assertTrue(math.isfinite(e.assistant.lead_target))

    def test_valid_command_still_applies(self):
        e = Engine(sim=True)
        e.command({"type": "fader", "ch": 2, "db": -7.0})
        self.assertAlmostEqual(e.assistant.fader_db[2], -7.0, places=1)


class TestStateMachine(unittest.TestCase):
    def test_calibration_refused_during_takeover(self):
        e = Engine(sim=True)
        e.panic(True)
        self.assertEqual(e.status, "takeover")
        e.run_calibration()
        self.assertEqual(e.calib_status, "none")     # never started actuating
        self.assertEqual(e.status, "takeover")


class TestScalingDomain(unittest.TestCase):
    def test_db_to_fader_always_in_range(self):
        for db in [-300, -90, -12, 0, 6, 10, 50, 1e9]:
            f = osc.db_to_fader(db)
            self.assertGreaterEqual(f, 0.0)
            self.assertLessEqual(f, 1.0)

    def test_freq_and_q_params_never_blow_up(self):
        for hz in [0, -5, 1, 5, 30000, 1e9]:
            p = osc.freq_to_eq_param(hz)
            self.assertTrue(0.0 <= p <= 1.0)
        for q in [0, -1, 0.3, 8, 100]:
            p = osc.q_to_param(q)
            self.assertTrue(math.isfinite(p))

    def test_detect_ring_ignores_dc(self):
        mag = np.zeros(513)
        mag[0] = 1000.0
        self.assertIsNone(dsp.detect_ring(mag))


class TestConcurrency(unittest.TestCase):
    def test_hammer_from_many_threads_no_races(self):
        # Runs the engine loop while many handler-like threads mutate state and
        # serialize telemetry concurrently. Before the single-lock refactor this
        # intermittently raised "deque/dict mutated during iteration".
        e = Engine(sim=True, tick=0.005)
        e.start()
        errors = []
        stop = threading.Event()

        def hammer(seq):
            i = 0
            try:
                while not stop.is_set():
                    e.command(seq[i % len(seq)])
                    i += 1
            except Exception as ex:          # any race surfaces here
                errors.append(repr(ex))

        def reader():
            try:
                while not stop.is_set():
                    json.dumps(e.snapshot())
            except Exception as ex:
                errors.append(repr(ex))

        seqs = [
            [{"type": "fader", "ch": 1, "db": -3}, {"type": "fader", "ch": 1, "db": 2}],
            [{"type": "mute", "ch": 3, "on": True}, {"type": "mute", "ch": 3, "on": False}],
            [{"type": "toggle", "job": "vocal_ride", "on": True},
             {"type": "toggle", "job": "balance", "on": True}],
            [{"type": "master", "db": -5}, {"type": "reset_notches"},
             {"type": "panic", "on": True}, {"type": "panic", "on": False}],
        ]
        threads = [threading.Thread(target=hammer, args=(s,)) for s in seqs]
        threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        time.sleep(0.6)
        stop.set()
        for t in threads:
            t.join(timeout=3)
        alive = e._thread.is_alive()
        e.stop()
        self.assertEqual(errors, [], f"concurrency races: {errors}")
        self.assertTrue(alive, "engine loop thread died under load")
        self.assertTrue(json.dumps(e.snapshot()))     # still serializable


if __name__ == "__main__":
    unittest.main()
