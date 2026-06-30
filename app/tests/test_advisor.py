import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix.advisor import Advisor
from trio_mix.engine import Engine
from trio_mix.sessionlog import SessionLog


class TestSessionLog(unittest.TestCase):
    def test_log_summary_recent(self):
        sl = SessionLog(":memory:")
        try:
            sl.start_session(venue="The Cellar", template="Trio", mode="simulation")
            sl.log_event({"kind": "feedback", "level": "notice", "ch": 1,
                          "role": "lead_vox", "msg": "2500 Hz"})
            sl.log_event({"kind": "clip", "level": "warn", "ch": 6,
                          "role": "cajon", "msg": "near clip"})
            sl.log_event({"kind": "feedback", "level": "notice", "ch": 8,
                          "role": "meas_mic", "msg": "ring"})
            sl.flush()
            self.assertEqual(sl.summary(), {"feedback": 2, "clip": 1})
            rec = sl.recent(10)
            self.assertEqual(len(rec), 3)
            self.assertEqual(rec[-1]["kind"], "feedback")
        finally:
            sl.close()

    def test_log_before_session_is_noop(self):
        sl = SessionLog(":memory:")
        try:
            sl.log_event({"kind": "x", "msg": "y"})    # no session yet -> dropped silently
            sl.flush()
            self.assertEqual(sl.recent(5), [])
        finally:
            sl.close()

    def test_venue_history(self):
        sl = SessionLog(":memory:")
        try:
            sl.start_session(venue="Barn", template="Trio")
            sl.log_event({"kind": "feedback", "msg": "a"})
            sl.flush()
            sl.start_session(venue="Barn", template="Trio")    # second show, same venue
            sl.log_event({"kind": "clip", "msg": "b"})
            sl.flush()
            hist = sl.venue_history("Barn")
            self.assertEqual(len(hist), 2)
            self.assertIn("counts", hist[0])
        finally:
            sl.close()


def _fake_caller(advice):
    return lambda ctx: dict(advice)


class TestAdvisor(unittest.TestCase):
    def test_unavailable_without_key_or_caller(self):
        a = Advisor(get_context=lambda: {}, on_advice=lambda x: None, api_key=None)
        # may be available if the env has a key; force the no-key case:
        a.api_key = None
        a.available = a._injected or bool(a.api_key)
        self.assertFalse(a.available)
        self.assertFalse(a.start())

    def test_injected_caller_is_available_and_ticks(self):
        got = []
        a = Advisor(get_context=lambda: {"recent_events": []},
                    on_advice=lambda adv: got.append(adv),
                    caller=_fake_caller({"performer_prompt": "Ease the guitar",
                                         "room_assessment": "2.5 kHz settling",
                                         "severity": "notice"}))
        self.assertTrue(a.available)
        out = a.tick()
        self.assertEqual(out["performer_prompt"], "Ease the guitar")
        self.assertEqual(got[0]["severity"], "notice")
        self.assertEqual(a.calls, 1)

    def test_background_loop_fires(self):
        got = []
        a = Advisor(get_context=lambda: {"recent_events": []},
                    on_advice=lambda adv: got.append(adv),
                    caller=_fake_caller({"performer_prompt": "ok", "severity": "info"}),
                    interval=0.2)
        self.assertTrue(a.start())
        try:
            deadline = time.monotonic() + 2.0
            while not got and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(got, "advisor background loop did not fire")
        finally:
            a.stop()

    def test_caller_error_is_swallowed(self):
        def boom(ctx):
            raise RuntimeError("network down")
        a = Advisor(get_context=lambda: {}, on_advice=lambda x: None, caller=boom)
        self.assertIsNone(a.tick())            # no raise
        self.assertIn("network down", a.last_error)

    def test_parse_extracts_json(self):
        p = Advisor.parse
        good = 'Sure! {"performer_prompt":"Lower the cajon","room_assessment":"boomy",'\
               '"severity":"warn"} done'
        d = p(good)
        self.assertEqual(d["performer_prompt"], "Lower the cajon")
        self.assertEqual(d["severity"], "warn")
        self.assertIsNone(p("no json here"))
        self.assertEqual(p('{"severity":"bogus"}')["severity"], "info")  # clamped


class TestEngineAdvisorIntegration(unittest.TestCase):
    def test_context_and_on_advice(self):
        e = Engine(sim=True)
        ctx = e.advisor_context()
        self.assertIn("recent_events", ctx)
        self.assertIn("channels", ctx)
        e.on_advice({"performer_prompt": "Watch the bass", "room_assessment": "ok",
                     "severity": "notice"})
        self.assertEqual(e.telemetry["advisor"]["prompt"], "Watch the bass")
        self.assertTrue(any(ev["kind"] == "advisor" for ev in list(e.events)))

    def test_empty_prompt_does_not_log_event(self):
        e = Engine(sim=True)
        e.on_advice({"performer_prompt": "", "room_assessment": "all good",
                     "severity": "info"})
        self.assertFalse(any(ev["kind"] == "advisor" for ev in list(e.events)))
        self.assertEqual(e.telemetry["advisor"]["assessment"], "all good")

    def test_session_log_receives_events(self):
        e = Engine(sim=True)
        sl = SessionLog(":memory:")
        sl.start_session(template="Trio")
        e.session_log = sl
        try:
            e.assistant._emit("feedback", "2500 Hz on lead", 1)
            sl.flush()
            self.assertEqual(sl.summary().get("feedback"), 1)
        finally:
            sl.close()


if __name__ == "__main__":
    unittest.main()
