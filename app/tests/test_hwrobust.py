"""Hardware-robustness tests: detection + handling + UX feedback for the failure
modes likely on a real gig (mac mic-permission silence, mid-show unplug + recovery,
port-in-use, too-few-channels, xrun overload, clearer error text)."""
import os
import sys
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trio_mix import config as C
from trio_mix.capture import (CaptureError, CaptureSource, SoundDeviceCapture,
                              _pick_input, _format_device_list, parse_channel_map)
from trio_mix.engine import Engine


class _FakeStream:
    def __init__(self, cb):
        self.cb = cb
    def start(self): pass
    def stop(self): pass
    def close(self): pass


def _cap():
    return SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb))


# ---------------------------------------------------------------------------
# capture.py — detection
# ---------------------------------------------------------------------------
class TestSilentInputDetection(unittest.TestCase):
    def test_sustained_silence_flagged(self):
        cap = _cap(); cap.start()
        cap._on_audio(np.zeros((C.BLOCK, cap.ndev), "float32"))
        cap.block()
        self.assertFalse(cap.silent())                    # hold not elapsed yet
        cap._silent_since = time.monotonic() - (cap.SILENCE_HOLD_S + 1)
        self.assertTrue(cap.silent())                     # silent long enough
        self.assertTrue(cap.status()["silent"])

    def test_signal_clears_silence(self):
        cap = _cap(); cap.start()
        cap._on_audio(np.zeros((C.BLOCK, cap.ndev), "float32")); cap.block()
        cap._silent_since = time.monotonic() - 10
        self.assertTrue(cap.silent())
        cap._on_audio(np.full((C.BLOCK, cap.ndev), 0.3, "float32")); cap.block()
        self.assertFalse(cap.silent())                    # real signal resets it

    def test_dead_supersedes_silent(self):
        cap = _cap(); cap.start()
        for _ in range(cap.SILENCE_AFTER + 2):            # starve -> dead
            cap.block()
        self.assertTrue(cap.dead())
        self.assertFalse(cap.silent())                    # dead, not "silent"


class TestOverloadDetection(unittest.TestCase):
    def test_high_xrun_rate_flagged(self):
        cap = _cap(); cap.start()
        cap._ovr_mark_t = time.monotonic() - 3.0
        cap._ovr_mark_val = 0
        cap._overruns = 100                               # 100 xruns / 3 s = 33/s
        self.assertTrue(cap.overload())
        self.assertTrue(cap.status()["overload"])


class TestPreflightChannelCheck(unittest.TestCase):
    def test_too_few_channels_raises_clear_error(self):
        try:
            import sounddevice as sd
        except ImportError:
            self.skipTest("sounddevice not installed")
        cap = SoundDeviceCapture(device=0)                # needs ndev (8) channels
        with mock.patch.object(sd, "query_devices",
                               return_value={"name": "Tiny 2in", "max_input_channels": 2}):
            with self.assertRaises(CaptureError) as cm:
                cap._make_input_stream(lambda *a: None)
        self.assertIn("input channel", str(cm.exception))
        self.assertIn("Tiny 2in", str(cm.exception))

    def test_device_not_found_raises_clear_error(self):
        try:
            import sounddevice as sd
        except ImportError:
            self.skipTest("sounddevice not installed")
        cap = SoundDeviceCapture(device=999)
        with mock.patch.object(sd, "query_devices", side_effect=ValueError("no such device")):
            with self.assertRaises(CaptureError) as cm:
                cap._make_input_stream(lambda *a: None)
        self.assertIn("--list-devices", str(cm.exception))


class TestStreamRestart(unittest.TestCase):
    def test_restart_revives_dead_stream(self):
        cap = _cap(); cap.start()
        for _ in range(cap.SILENCE_AFTER + 2):
            cap.block()
        self.assertTrue(cap.dead())
        self.assertTrue(cap.restart())
        self.assertFalse(cap.dead())                      # counters reset


# ---------------------------------------------------------------------------
# engine.py — handling + UX
# ---------------------------------------------------------------------------
class _SilentMic(CaptureSource):
    def block(self):
        return {ch: np.zeros(C.BLOCK, "float32") for ch in C.CHANNELS}
    def status(self):
        return {"kind": "audio", "silent": True}


class _FlakyMic(CaptureSource):
    def __init__(self):
        self._dead = True
        self.restarts = 0
    def block(self):
        return {ch: np.zeros(C.BLOCK, "float32") for ch in C.CHANNELS}
    def dead(self):
        return self._dead
    def restart(self):
        self.restarts += 1
        self._dead = False
        return True


class _FailToOpenMic(CaptureSource):
    def start(self):
        raise CaptureError("audio device 'Tiny 2in' has 2 input channels but needs 8")
    def block(self):
        return {ch: np.zeros(C.BLOCK, "float32") for ch in C.CHANNELS}


class _LateMic(CaptureSource):
    """Busy/late at launch: start() fails the first N times, then succeeds;
    reports dead() until started, so the watchdog re-opens it (no relaunch)."""
    def __init__(self, fail_starts=1):
        self._fails = fail_starts
        self._started = False
        self.starts = 0
    def start(self):
        self.starts += 1
        if self._fails > 0:
            self._fails -= 1
            raise CaptureError("audio device busy (still powering up)")
        self._started = True
    def block(self):
        return {ch: np.zeros(C.BLOCK, "float32") for ch in C.CHANNELS}
    def dead(self):
        return not self._started
    def restart(self):
        try:
            self.start(); return True
        except Exception:
            return False


class _BadRx:
    def start(self):
        raise OSError("address already in use")
    def stop(self):
        pass


class TestEngineHardwareUX(unittest.TestCase):
    def test_silent_input_raises_warn_alert(self):
        e = Engine(sim=False, source=_SilentMic())
        self.assertEqual(e.telemetry["op_mode"], "alert")
        self.assertTrue(any(a["level"] == "warn" and "silent" in a["msg"].lower()
                            for a in e.telemetry["alerts"]))

    def test_dead_stream_auto_recovers(self):
        e = Engine(sim=False, source=_FlakyMic())
        e._recover_audio(time.monotonic())
        self.assertEqual(e.source.restarts, 1)
        self.assertTrue(any("recovered" in ev["msg"] for ev in list(e.events)))
        # backoff: an immediate second call (still <3s) does not retry
        e.source._dead = True
        e._recover_audio(time.monotonic())
        self.assertEqual(e.source.restarts, 1)

    def test_audio_open_failure_keeps_source_for_retry(self):
        e = Engine(sim=False, source=_FailToOpenMic())
        e.start()
        try:
            self.assertIsNotNone(e.source)                # kept so the watchdog retries
            self.assertTrue(any("audio capture off" in ev["msg"] and "Tiny 2in" in ev["msg"]
                                and "retrying" in ev["msg"] for ev in list(e.events)))
        finally:
            e.stop()

    def test_busy_at_launch_then_recovers(self):
        src = _LateMic(fail_starts=1)                     # first open fails, then OK
        e = Engine(sim=False, source=src)
        e.start()                                         # launch-time open fails, source kept
        try:
            self.assertIsNotNone(e.source)
            e._last_audio_restart = -1e9                  # bypass backoff
            e._recover_audio(time.monotonic())            # watchdog re-opens it
            self.assertTrue(src._started)
            self.assertTrue(any("recovered" in ev["msg"] for ev in list(e.events)))
        finally:
            e.stop()

    def test_watchdog_skips_during_shutdown(self):
        src = _FlakyMic()
        e = Engine(sim=False, source=src)
        e._stop.set()                                     # shutting down
        e._recover_audio(time.monotonic())
        self.assertEqual(src.restarts, 0)                 # no re-open while stopping

    def test_output_device_wired(self):
        cap = SoundDeviceCapture(device=0, output_device=3)
        self.assertEqual(cap.output_device, 3)

    def test_input_gain_boosts_capture(self):
        cap = SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb), gain_db=20.0)
        cap.start()
        cap._on_audio(np.full((C.BLOCK, cap.ndev), 0.05, "float32"))   # 20 dB == 10x
        out = cap.block()
        self.assertAlmostEqual(float(out[1][0]), 0.5, places=3)        # 0.05 * 10
        cap2 = SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb))   # default no gain
        cap2.start(); cap2._on_audio(np.full((C.BLOCK, cap2.ndev), 0.05, "float32"))
        self.assertAlmostEqual(float(cap2.block()[1][0]), 0.05, places=3)

    def test_meter_port_busy_degrades_not_crashes(self):
        e = Engine(sim=True)
        e.meter_rx = _BadRx()
        e.start()                                         # must not raise
        try:
            self.assertIsNone(e.meter_rx)
            self.assertTrue(any("meter port busy" in ev["msg"] for ev in list(e.events)))
        finally:
            e.stop()


class TestAutoDetect(unittest.TestCase):
    def test_pick_prefers_native_rate_and_modern_api(self):
        apis = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]
        devs = [
            {"name": "Mic (Q9U)", "max_input_channels": 1, "default_samplerate": 44100, "hostapi": 0},
            {"name": "Speakers", "max_input_channels": 0, "default_samplerate": 48000, "hostapi": 0},
            {"name": "Mic (Q9U)", "max_input_channels": 1, "default_samplerate": 44100, "hostapi": 1},
            {"name": "Mic (Q9U)", "max_input_channels": 1, "default_samplerate": 48000, "hostapi": 2},
        ]
        pick = _pick_input(devs, apis, "Mic (Q9U)", prefer_sr=48000)
        self.assertEqual(pick["index"], 3)            # the 48k WASAPI duplicate wins
        self.assertEqual(pick["samplerate"], 48000)
        self.assertEqual(pick["channels"], 1)

    def test_pick_none_when_no_inputs(self):
        devs = [{"name": "Spk", "max_input_channels": 0, "default_samplerate": 48000, "hostapi": 0}]
        self.assertIsNone(_pick_input(devs, [{"name": "MME"}], None))

    def test_external_mic_beats_builtin_array_even_as_default(self):
        apis = [{"name": "Windows WASAPI"}]
        devs = [
            {"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 2,
             "default_samplerate": 48000, "hostapi": 0},                       # OS default
            {"name": "Microphone (Samson Q9U)", "max_input_channels": 1,
             "default_samplerate": 48000, "hostapi": 0},                       # plugged-in USB
        ]
        pick = _pick_input(devs, apis, "Microphone Array (Realtek(R) Audio)", prefer_sr=48000)
        self.assertIn("Q9U", pick["name"])             # external mic wins over built-in array

    def test_builtin_used_when_only_input(self):
        apis = [{"name": "Windows WASAPI"}]
        devs = [{"name": "Microphone Array (Realtek)", "max_input_channels": 2,
                 "default_samplerate": 48000, "hostapi": 0}]
        self.assertIsNotNone(_pick_input(devs, apis, None))   # still picked if it's all there is

    def test_asio_beats_wasapi_for_same_interface(self):
        # A pro interface (console X-USB) shows up under both WASAPI and ASIO;
        # ASIO is the lower-latency native path and must win the tiebreak.
        apis = [{"name": "Windows WASAPI"}, {"name": "ASIO"}]
        devs = [
            {"name": "X-USB", "max_input_channels": 32, "default_samplerate": 48000, "hostapi": 0},
            {"name": "X-USB", "max_input_channels": 32, "default_samplerate": 48000, "hostapi": 1},
        ]
        pick = _pick_input(devs, apis, None, prefer_sr=48000)
        self.assertEqual(pick["hostapi"], "ASIO")


class TestDeviceListFormatting(unittest.TestCase):
    def test_list_shows_hostapi_and_only_inputs(self):
        apis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
        devs = [
            {"name": "X-USB", "max_input_channels": 32, "default_samplerate": 48000, "hostapi": 1},
            {"name": "Speakers", "max_input_channels": 0, "default_samplerate": 48000, "hostapi": 0},
            {"name": "Q9U", "max_input_channels": 1, "default_samplerate": 44100, "hostapi": 0},
        ]
        out = _format_device_list(devs, apis)
        self.assertIn("[0] X-USB  (32 in, 48000 Hz, Windows WASAPI)", out)
        self.assertIn("[2] Q9U  (1 in, 44100 Hz, MME)", out)
        self.assertNotIn("Speakers", out)                 # output-only device omitted

    def test_empty_list(self):
        self.assertIn("(none found)", _format_device_list([], []))


class TestChannelMapParsing(unittest.TestCase):
    def test_valid_map(self):
        self.assertEqual(parse_channel_map("1:0,2:1,8:9"), {1: 0, 2: 1, 8: 9})

    def test_whitespace_and_trailing_comma(self):
        self.assertEqual(parse_channel_map(" 1:0 , 2:1 ,"), {1: 0, 2: 1})

    def test_custom_map_reaches_capture(self):
        cap = SoundDeviceCapture(device=0, channel_map=parse_channel_map("1:0,8:9"))
        self.assertEqual(cap.channel_map, {1: 0, 8: 9})
        self.assertEqual(cap.ndev, 10)                    # opens columns 0..9 (max+1)

    def test_bad_maps_raise(self):
        for bad in ("", "8", "8:x", "0:0", "8:-1", "  "):
            with self.assertRaises(ValueError):
                parse_channel_map(bad)


class TestAutoChannelMap(unittest.TestCase):
    def setUp(self):
        self._snap = C.channel_map_state()

    def tearDown(self):
        C.restore_channel_map_state(self._snap)

    def test_one_input_is_meas_only(self):
        C.auto_channel_map(1)
        self.assertEqual(C.CHANNELS, {1: "meas_mic"})
        self.assertEqual(C.MEAS_MIC_CH, 1)
        self.assertEqual(C.BALANCE_CHANNELS, ())

    def test_n_inputs_last_is_meas(self):
        C.auto_channel_map(4)
        self.assertEqual(len(C.CHANNELS), 4)
        self.assertEqual(C.CHANNELS[4], "meas_mic")
        self.assertEqual(C.MEAS_MIC_CH, 4)


class TestMeasChannelFollowsMap(unittest.TestCase):
    """Regression: capture_meas must read the meas channel the ACTIVE map declares,
    not a frozen import-time default. Broke --auto/bench maps where meas_mic != 8."""
    def setUp(self):
        self._snap = C.channel_map_state()

    def tearDown(self):
        C.restore_channel_map_state(self._snap)

    def test_meas_ch_tracks_auto_map(self):
        C.auto_channel_map(1)                          # meas_mic moves to channel 1
        cap = SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb))
        self.assertEqual(cap.meas_ch, 1)
        self.assertIsNotNone(cap.channel_map.get(cap.meas_ch))
        cap.start()
        for _ in range(4):
            cap._on_audio(np.full((C.BLOCK, cap.ndev), 0.2, "float32"))
        rec = cap.capture_meas(0.05)
        self.assertIsNotNone(rec)                      # was None with the frozen default
        self.assertTrue(np.allclose(rec, 0.2))


class TestAutoGain(unittest.TestCase):
    def test_auto_gain_boosts_quiet_mic_toward_target(self):
        cap = SoundDeviceCapture(stream_factory=lambda cb: _FakeStream(cb))
        cap.start()
        for _ in range(32):                           # fill with a quiet -40 dBFS signal
            cap._on_audio(np.full((C.BLOCK, cap.ndev), 0.01, "float32"))
        g = cap.auto_gain(target_dbfs=-20.0, seconds=0.08)
        self.assertGreater(g, 12.0)                   # ~ -40 -> -20 needs ~+20 dB
        self.assertLess(g, 28.0)


if __name__ == "__main__":
    unittest.main()
