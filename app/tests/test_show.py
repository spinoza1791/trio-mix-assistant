import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix import template as tmpl
from trio_mix.capture import CaptureSource
from trio_mix.engine import Engine
from trio_mix.showclock import AbleSetReceiver, ShowState, SimShowClock


class TestTemplate(unittest.TestCase):
    def test_default_loads(self):
        t = tmpl.default_template()
        self.assertTrue(len(t.songs) >= 3)
        self.assertEqual(t.songs[0].scene, 1)
        self.assertAlmostEqual(t.songs[0].lead_target, -6.0)

    def test_lookup_case_insensitive(self):
        t = tmpl.default_template()
        self.assertEqual(t.song_by_name("gravity").name, "Gravity")
        self.assertIsNone(t.song_by_name("nope"))
        self.assertIsNone(t.song_by_name(""))
        self.assertEqual(t.song_at(0).name, "Gravity")
        self.assertIsNone(t.song_at(99))

    def test_balance_channels_validated(self):
        t = tmpl.from_dict({"songs": [{"name": "A", "scene": 1, "balance": {"5": -20.0}}]})
        self.assertEqual(t.songs[0].balance[5], -20.0)

    def test_invalid_raises(self):
        for bad in (
            {"songs": [{"scene": 1}]},                              # missing name
            {"songs": [{"name": "A", "scene": "two"}]},             # scene not int
            {"songs": [{"name": "A", "balance": {"99": -3}}]},      # channel not in map
            {"songs": [{"name": "A"}, {"name": "a"}]},              # duplicate (case-insens)
            {"songs": [{"name": "A", "lead_target": "loud"}]},      # non-numeric
            {"songs": "nope"},                                      # songs not a list
        ):
            with self.assertRaises(tmpl.TemplateError):
                tmpl.from_dict(bad)


class TestSimShowClock(unittest.TestCase):
    def test_advance_cycles(self):
        t = tmpl.default_template()
        seen = []
        clk = SimShowClock(t, on_change=lambda st: seen.append(st))
        clk._advance(); clk._advance()
        self.assertEqual(seen[0].song_name, "Gravity")
        self.assertEqual(seen[0].next_song, "Bloom")
        self.assertEqual(seen[1].song_name, "Bloom")
        self.assertTrue(seen[0].playing)

    def test_start_stop(self):
        t = tmpl.default_template()
        seen = []
        clk = SimShowClock(t, on_change=lambda st: seen.append(st), interval=10)
        clk.start()
        time.sleep(0.2)            # fires song 1 immediately
        clk.stop()
        self.assertGreaterEqual(len(seen), 1)


class TestAbleSetReceiver(unittest.TestCase):
    def _rx(self, sink):
        rx = AbleSetReceiver(on_change=lambda st: sink.append(st))
        rx._running = True
        return rx

    def test_song_change_fires_once(self):
        sink = []
        rx = self._rx(sink)
        rx.h_song_index("/setlist/activeSongIndex", 2)
        rx.h_next_song("/setlist/nextSongName", "Bloom")
        rx.h_song_name("/setlist/activeSongName", "Gravity")
        rx.h_song_name("/setlist/activeSongName", "Gravity")   # same -> no second fire
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0].song_name, "Gravity")
        self.assertEqual(sink[0].song_index, 2)
        self.assertEqual(sink[0].next_song, "Bloom")

    def test_section_and_playing_fire(self):
        sink = []
        rx = self._rx(sink)
        rx.h_song_name("/setlist/activeSongName", "Gravity")   # fire 1 (playing False)
        rx.h_section("/song/sectionName", "Chorus")            # fire 2 (section change)
        rx.h_playing("/playback/isPlaying", 1)                 # fire 3 (started -> change)
        self.assertEqual(sink[-1].section, "Chorus")
        self.assertTrue(sink[-1].playing)
        self.assertEqual(len(sink), 3)
        rx.h_playing("/playback/isPlaying", 1)                 # no change -> no extra fire
        self.assertEqual(len(sink), 3)

    def test_noop_after_stop(self):
        sink = []
        rx = AbleSetReceiver(on_change=lambda st: sink.append(st))
        rx._running = False
        rx.h_song_name("/setlist/activeSongName", "Gravity")
        self.assertEqual(sink, [])


class TestEngineSceneRecall(unittest.TestCase):
    def _state(self, name, index=0, nxt="", section="Song"):
        return ShowState(song_index=index, song_name=name, next_song=nxt,
                         section=section, playing=True)

    def test_song_change_recalls_scene_and_applies_levels(self):
        e = Engine(sim=True)
        e.on_song_change(self._state("Bloom", index=1, nxt="Wildflower"))
        self.assertEqual(e.con.last_scene, 2)                  # scene recalled
        self.assertAlmostEqual(e.assistant.lead_target, -7.0)  # per-song lead target
        self.assertEqual(e.assistant.balance_targets.get(5), -21.0)
        self.assertEqual(e.telemetry["show"]["current"], "Bloom")
        self.assertEqual(e.telemetry["show"]["scene"], 2)
        self.assertEqual(e.telemetry["show"]["next"], "Wildflower")

    def test_same_song_does_not_re_recall(self):
        e = Engine(sim=True)
        e.on_song_change(self._state("Gravity"))
        e.con.last_scene = 99                                  # sentinel
        e.on_song_change(self._state("Gravity", section="Chorus"))  # same song, new section
        self.assertEqual(e.con.last_scene, 99)                 # no recall happened
        self.assertEqual(e.telemetry["show"]["section"], "Chorus")  # but telemetry updated

    def test_unknown_song_leaves_mix(self):
        e = Engine(sim=True)
        e.con.last_scene = 42
        e.on_song_change(self._state("Encore Jam"))
        self.assertEqual(e.con.last_scene, 42)                 # no scene recall
        self.assertTrue(any("no template entry" in ev["msg"] for ev in list(e.events)))

    def test_takeover_blocks_scene_recall(self):
        e = Engine(sim=True)
        e.con.last_scene = 7
        e.status = "takeover"
        e.on_song_change(self._state("Bloom", index=1))
        self.assertEqual(e.con.last_scene, 7)                  # scene NOT recalled in takeover
        self.assertAlmostEqual(e.assistant.lead_target, -7.0)  # internal target still set

    def test_manual_recall_blocked_in_takeover(self):
        e = Engine(sim=True)
        e.con.last_scene = 5
        e.status = "takeover"
        e.recall_scene_manual(2)
        self.assertEqual(e.con.last_scene, 5)                  # blocked
        self.assertTrue(any("blocked" in ev["msg"] for ev in list(e.events)))

    def test_dedup_normalizes_song_name(self):
        e = Engine(sim=True)
        e.on_song_change(self._state("Gravity"))
        e.con.last_scene = 99                                  # sentinel
        e.on_song_change(self._state(" gravity "))            # same song, casing/space
        self.assertEqual(e.con.last_scene, 99)                 # not re-recalled


class _DeadSource(CaptureSource):
    def block(self):
        return {ch: np.zeros(C.BLOCK, "float32") for ch in C.CHANNELS}
    def status(self):
        return {"kind": "audio", "dead": True}


class TestPerformerUI(unittest.TestCase):
    def test_op_mode_auto_alert_manual(self):
        e = Engine(sim=True)
        self.assertEqual(e.telemetry["op_mode"], "auto")
        with e._lock:
            e.status = "takeover"
            e._rebuild_telemetry()
        self.assertEqual(e.telemetry["op_mode"], "manual")

    def test_dead_capture_raises_alert(self):
        e = Engine(sim=False, source=_DeadSource())
        self.assertEqual(e.telemetry["op_mode"], "alert")      # a brief dead -> alert (warn)
        with e._lock:                                          # sustained dead -> critical
            e._dead_since = time.monotonic() - 5.0
            e._rebuild_telemetry()
        self.assertIn("critical", [a["level"] for a in e.telemetry["alerts"]])

    def test_scenes_list_and_manual_recall(self):
        e = Engine(sim=True)
        scenes = e.telemetry["scenes"]
        self.assertEqual([s["index"] for s in scenes], [1, 2, 3, 4])
        self.assertFalse(any(s["current"] for s in scenes))
        e.recall_scene_manual(3)
        self.assertEqual(e.con.last_scene, 3)
        cur = [s for s in e.telemetry["scenes"] if s["current"]]
        self.assertEqual(cur[0]["index"], 3)

    def test_command_scene_type(self):
        e = Engine(sim=True)
        e.command({"type": "scene", "index": 2})
        self.assertEqual(e.con.last_scene, 2)
        e.command({"type": "scene", "index": "bad"})   # ignored, no raise
        self.assertEqual(e.con.last_scene, 2)

    def test_event_levels(self):
        L = Engine._level_for
        self.assertEqual(L({"kind": "feedback", "msg": "2500 Hz on lead"}), "notice")
        self.assertEqual(L({"kind": "vocal", "msg": "out -7"}), "info")
        self.assertEqual(L({"kind": "clip", "msg": "lead near clip"}), "warn")
        self.assertEqual(L({"kind": "clip", "msg": "lead clean -> restoring"}), "info")
        self.assertEqual(L({"kind": "system", "msg": "audio capture failed to start"}), "warn")
        self.assertEqual(L({"kind": "system", "msg": "the assistant is deaf"}), "critical")
        self.assertEqual(L({"kind": "show", "msg": "song: Bloom -> scene 2"}), "notice")

    def test_events_get_level_stamped(self):
        e = Engine(sim=True)
        e.assistant._emit("feedback", "2500 Hz on lead", 1)
        self.assertEqual(list(e.events)[-1]["level"], "notice")


if __name__ == "__main__":
    unittest.main()
