"""Runtime engine: a closed-loop simulator + the live telemetry the dashboard
reads. Wires SimStage -> DSP -> MixAssistant -> Console, one block at a time.

The simulator is a genuine closed loop: feedback rings grow until the assistant
parks a notch near them; a clipping channel recovers once its preamp is trimmed;
the lead vocal's input drifts and the ride compensates. So toggling jobs in the
UI produces visibly different behaviour — which is exactly what makes it testable.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque

import numpy as np


def _num(x) -> float:
    """Coerce to a finite float or raise — rejects NaN/Infinity from untrusted input."""
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def _clampf(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

from . import config as C
from . import dsp
from .assistant import MixAssistant
from .calibration import Calibrator, CalibrationResult
from .capture import CaptureError, CaptureSource, SimCapture, sim_room_capture  # noqa: F401
from .osc import ConsoleBase, SimConsole
from . import template as tmpl


# ---------------------------------------------------------------------------
# Engine  (the audio "listening half" lives in capture.py: SimCapture / SoundDeviceCapture)
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, console: ConsoleBase | None = None,
                 sim: bool = True, tick: float = 0.05,
                 source: CaptureSource | None = None,
                 template: "tmpl.ShowTemplate | None" = None) -> None:
        self.sim = sim
        self.tick = tick
        self.con = console or SimConsole()
        # The listening half: an explicit source wins; else SimCapture in sim mode;
        # else None (hardware with no audio device -> manual control + scene recall).
        if source is not None:
            self.source: CaptureSource | None = source
        elif isinstance(self.con, SimConsole):
            self.source = SimCapture(self.con)
        else:
            self.source = None
        self.assistant = MixAssistant(self.con, on_event=self._on_event,
                                      on_latency=self._note_latency_ev)
        self.calibrator = Calibrator(self.con)

        # One reentrant lock owns ALL engine/assistant/console state, the event
        # log, and the telemetry dict. The loop thread and every HTTP handler
        # thread mutate + read through it, so there are no races (no torn reads,
        # no deque-mutated-during-iteration, no dict-changed-size-during-build).
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        self._last_err_t = -1e9

        self.status = "live"                 # live | calibrating | takeover
        self.calib: CalibrationResult | None = None
        self.calib_status = "none"           # none | running | done
        self._enabled_backup: dict | None = None
        self._last_notch_t = {ch: -1e9 for ch in C.CHANNELS}
        self._last_clip_t = {ch: -1e9 for ch in C.CHANNELS}
        self.muted = {ch: False for ch in C.CHANNELS}
        self.master_db = 0.0
        # performer EQ / FX believed-state (operator-set; console stays the truth)
        mix_chans = [c for c in C.CHANNELS if c != C.MEAS_MIC_CH]
        freqs = list(C.EQ_DEFAULT_FREQS)
        self.eq = {c: {b: {"hz": freqs[b - 1] if b - 1 < len(freqs) else 1000.0,
                           "gain": 0.0, "q": 2.0, "on": False}
                       for b in range(1, C.CHANNEL_EQ_BANDS + 1)} for c in mix_chans}
        self.sends = {c: {fx: -90.0 for fx in C.FX_BUSES} for c in mix_chans}
        self.fx_wet = {fx: -90.0 for fx in C.FX_BUSES}
        self.meter_rx = None                 # MeterReceiver, attached by run.py (hardware)
        self.show_clock = None               # SimShowClock / AbleSetReceiver, attached by run.py
        self.session_log = None              # SessionLog (SQLite), attached by run.py
        self.advisor = None                  # Advisor (Claude slow layer), attached by run.py
        self.advisor_advice = None           # latest advisory note (for telemetry)
        self.venue = ""                      # venue name (for the learned model)
        self.venue_dir = None                # where per-venue models live
        self.venue_model = None              # loaded/learned VenueModel
        self.template = template if template is not None else tmpl.default_template()
        self._applied_song: str | None = None
        self._song_start_t = -1e9            # monotonic when the current song began (8-bar gate)
        self._dead_since = -1e9              # when audio capture first went dead (debounce)
        self.show = {"current": "", "next": "", "section": "", "bpm": None,
                     "scene": None, "playing": False, "index": None}
        self._renew_thread: threading.Thread | None = None
        self._meter_count = 0                # # of console meter values last seen (atomic int)
        self._last_rx_t = -1e9               # monotonic time we last heard from the console
        self._last_reconcile_log = {}        # ch -> last time we logged an external move
        self._feat: dict[int, dsp.ChannelFeatures] = {}
        self._meas_ring = dsp.RollingRingDetector()   # high-res meas-mic ring detection
        self._perf = {"tick_ms": 0.0, "jitter_ms": 0.0, "max_ms": 0.0}
        self._latency: dict[str, dict] = {}  # kind -> detect->actuate ms {last,ema,max,n}
        self._last_audio_restart = -1e9      # audio-recovery watchdog backoff
        self.events: deque = deque(maxlen=120)
        self.telemetry: dict = {}
        with self._lock:
            self._rebuild_telemetry()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.meter_rx is not None:
            try:
                self.meter_rx.start()       # listen before subscribing, so we catch replies
            except OSError as exc:          # the meter/reconcile UDP port is in use
                with self._lock:
                    self.assistant._emit("system",
                                         f"meter port busy ({type(exc).__name__}) — console "
                                         "fader moves won't sync (another instance running?)")
                    self._rebuild_telemetry()
                self.meter_rx = None        # degrade: no reconciliation, app still runs
        self.con.subscribe_meters()
        if self.source is not None:
            try:
                self.source.start()
                st = self.source.status()
                if st.get("name"):     # log the resolved device so a shifted index shows
                    with self._lock:
                        self.assistant._emit("system",
                                             f"listening on '{st['name']}' "
                                             f"({st.get('channels')} ch)")
                        self._rebuild_telemetry()
            except Exception as exc:    # device missing / too few channels / busy / perms
                detail = str(exc) if isinstance(exc, CaptureError) else \
                    f"{type(exc).__name__}: {exc}"
                with self._lock:
                    # Keep the source: the watchdog retries every few seconds, so a
                    # device that's busy / still powering up AT LAUNCH recovers
                    # without a relaunch (the "deaf" banner shows meanwhile). Manual
                    # mixing + scene recall keep working regardless.
                    self.assistant._emit("system",
                                         f"audio capture off — {detail} — retrying")
                    self._rebuild_telemetry()
        else:                           # no audio device: assistant can't listen
            with self._lock:
                self.assistant._emit("system",
                                     "no audio capture configured — automatic jobs "
                                     "inactive (manual mixing + scene recall only)")
                self._rebuild_telemetry()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if self.meter_rx is not None:       # renew /batchsubscribe + /xremote (~10 s TTL)
            self._renew_thread = threading.Thread(target=self._renew_loop, daemon=True)
            self._renew_thread.start()
        if self.show_clock is not None:     # start auto scene recall last (loop is up)
            try:
                self.show_clock.start()
            except Exception as exc:
                with self._lock:
                    self.assistant._emit("system",
                                         f"show clock unavailable ({type(exc).__name__}) "
                                         "— scenes won't recall automatically")
                    self._rebuild_telemetry()
                self.show_clock = None
        if self.advisor is not None and self.advisor.start():
            with self._lock:
                self.assistant._emit("system", "AI advisor active (advisory notes only)")
                self._rebuild_telemetry()

    def _renew_loop(self) -> None:
        while not self._stop.wait(8.0):
            try:
                self.con.subscribe_meters()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self.advisor is not None:
            self.advisor.stop()
        if self.show_clock is not None:
            self.show_clock.stop()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._renew_thread:
            self._renew_thread.join(timeout=2.0)
        if self.meter_rx is not None:
            self.meter_rx.stop()
        if self.source is not None:
            self.source.stop()
        if self.session_log is not None:
            self.learn_venue()           # post-show: update this venue's learned model
            self.session_log.close()
        self.con.close()

    def _run(self) -> None:
        next_t = time.monotonic()
        last_now = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                # block() is non-blocking for both sources (SimCapture computes;
                # SoundDeviceCapture drains its queue), so it's safe under the lock
                # — and SimCapture reads console state that the lock protects.
                with self._lock:
                    # During a calibration sweep, stop pulling frames entirely:
                    # on hardware the loop and capture_meas() share one audio
                    # queue, so draining here would steal the meas-mic recording.
                    if self.source is not None and self.status != "calibrating":
                        frame = self.source.block()
                        self.assistant.block_t0 = time.perf_counter()   # detect→actuate clock
                        for ch, samples in frame.items():
                            feat = dsp.analyse_block(samples)
                            if ch == C.MEAS_MIC_CH:
                                # refine the acoustic-feedback estimate on a longer
                                # rolling window (finer freq resolution) — the
                                # per-block rms/peak stay live off the current block.
                                feat.fb_freq, feat.harmonicity = self._meas_ring.update(samples)
                            self._feat[ch] = feat
                            self.assistant.on_features(ch, feat, now)
                    self._note_perf(now, last_now)
                    self._rebuild_telemetry()
                self._recover_audio(now)    # off-lock: re-open a dead device with backoff
            except Exception as exc:        # a bad block must never kill the engine
                self._note_error(exc)
            last_now = now
            next_t += self.tick
            time.sleep(max(0.0, next_t - time.monotonic()))

    AUDIO_RESTART_S = 3.0      # how often to retry re-opening a dead audio device

    def _recover_audio(self, now: float) -> None:
        """If the audio device went dead (unplugged/wedged), try to re-open it on
        a backoff. Runs OFF the engine lock because re-opening a PortAudio stream
        can take tens of ms. The 'deaf' alert stays up until frames flow again."""
        if self._stop.is_set():         # don't start a re-open while shutting down
            return
        src = self.source
        if src is None or not src.dead():
            return
        if now - self._last_audio_restart < self.AUDIO_RESTART_S:
            return
        self._last_audio_restart = now
        recovered = False
        try:
            recovered = src.restart()
        except Exception:
            recovered = False
        if recovered:
            with self._lock:
                self.assistant._emit("system", "audio capture recovered — listening again")
                self._rebuild_telemetry()

    def _note_latency_ev(self, kind: str, seconds: float) -> None:
        """Aggregate detect→actuate latency per actuation kind (called from the
        assistant on the loop thread; the per-channel work is already under the
        engine lock). This is the spec's 'event detected → OSC sent' delta —
        the in-process part; the audio block buffer (~BLOCK/SR) and the feedback
        sustain window add to true end-to-end."""
        if not (0.0 <= seconds < 5.0):       # guard a stale block_t0 / absurd delta
            return
        ms = seconds * 1000.0
        e = self._latency.get(kind)
        if e is None:
            self._latency[kind] = {"last": round(ms, 2), "ema": round(ms, 2),
                                   "max": round(ms, 2), "n": 1}
        else:
            a = 0.2
            e["last"] = round(ms, 2)
            e["ema"] = round((1 - a) * e["ema"] + a * ms, 2)
            e["max"] = round(max(e["max"], ms), 2)
            e["n"] += 1

    def _note_perf(self, now: float, last_now: float) -> None:
        """Track loop responsiveness for the spec's latency success criteria:
        compute time per tick + how far the actual period drifts from `tick`
        (jitter). Cheap EMAs; caller holds the lock."""
        compute_ms = (time.monotonic() - now) * 1000.0
        period_ms = (now - last_now) * 1000.0
        jitter_ms = abs(period_ms - self.tick * 1000.0)
        a = 0.1
        self._perf["tick_ms"] = round((1 - a) * self._perf["tick_ms"] + a * compute_ms, 2)
        self._perf["jitter_ms"] = round((1 - a) * self._perf["jitter_ms"] + a * jitter_ms, 2)
        self._perf["max_ms"] = round(max(self._perf["max_ms"] * 0.999, compute_ms), 2)

    def _note_error(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_err_t < 5.0:    # throttle so one bug can't spam the log
            return
        self._last_err_t = now
        try:
            with self._lock:
                self.assistant._emit("system",
                                     f"engine error (recovered): {type(exc).__name__}: {exc}")
                self._rebuild_telemetry()
        except Exception:
            pass

    # -- events -------------------------------------------------------------
    @staticmethod
    def _level_for(ev: dict) -> str:
        """Map an event to one of 4 notification levels (the performer UI shows
        warn/critical as banners; info/notice live only in the log)."""
        kind, msg = ev["kind"], ev["msg"].lower()
        if any(w in msg for w in ("failed", "unavailable", "stopped", "deaf",
                                  "near-silence", "lost", "aborted")):
            return "critical" if ("deaf" in msg or "stopped" in msg) else "warn"
        if kind in ("vocal", "balance"):
            return "info"
        if kind in ("feedback", "manual", "show", "calibration", "advisor"):
            return "notice"
        if kind == "clip":
            return "warn" if "near clip" in msg else "info"
        return "info"

    def _on_event(self, ev: dict) -> None:
        now = time.monotonic()
        ev["level"] = self._level_for(ev)
        if ev["kind"] == "feedback" and ev["ch"] is not None:
            self._last_notch_t[ev["ch"]] = now
        if ev["kind"] == "clip" and ev["ch"] is not None and "near clip" in ev["msg"]:
            self._last_clip_t[ev["ch"]] = now
        self.events.append(ev)
        if self.session_log is not None:        # enqueue only — never touches disk here
            self.session_log.log_event(ev)

    # -- operator controls (any thread; serialized by self._lock) -----------
    def set_enabled(self, job: str, on: bool) -> None:
        with self._lock:
            if job not in self.assistant.enabled:
                return
            self.assistant.enabled[job] = bool(on)
            if job == "balance" and on:
                self.assistant.capture_balance(self._output_levels())
            self.assistant._emit("system", f"{job} {'ENABLED' if on else 'disabled'}")
            self._rebuild_telemetry()

    def set_coach_mode(self, on: bool) -> None:
        """Toggle the deterministic coach: jobs advise manual moves instead of
        actuating the console. No AI — the moves are the same math as auto mode."""
        with self._lock:
            self.assistant.set_coach_mode(bool(on))
            self._rebuild_telemetry()

    def set_lead_target(self, db: float) -> None:
        try:
            db = _num(db)
        except (TypeError, ValueError):
            return
        with self._lock:
            self.assistant.lead_target = db
            self.assistant._emit("system", f"lead target set to {db:+.1f} dB")
            self._rebuild_telemetry()

    # -- manual mixing surface ---------------------------------------------
    def set_fader(self, ch: int, db: float) -> None:
        if ch not in C.CHANNELS:
            return
        try:
            db = _num(db)
        except (TypeError, ValueError):
            return
        with self._lock:
            now = time.monotonic()
            db = self.con.set_fader_raw(ch, db)
            self.assistant.fader_db[ch] = db
            self.assistant.note_manual(ch, now)            # auto yields briefly
            self.assistant.last_move_t[ch] = now           # reconciliation ignores our echo
            self.assistant._emit("manual", f"{C.CHANNELS[ch]} fader → {db:+.1f} dB", ch)
            partner = self._linked_partner(ch)             # stereo link mirrors the move
            if partner is not None:
                pdb = self.con.set_fader_raw(partner, db)
                self.assistant.fader_db[partner] = pdb
                self.assistant.note_manual(partner, now)
                self.assistant.last_move_t[partner] = now
                self.assistant._emit("manual",
                                     f"{C.CHANNELS[partner]} linked → {pdb:+.1f} dB", partner)
            self._rebuild_telemetry()

    @staticmethod
    def _linked_partner(ch: int):
        for pair in C.STEREO_LINKS:
            if ch in pair:
                for other in pair:
                    if other != ch and other in C.CHANNELS:
                        return other
        return None

    # -- performer EQ / FX surface -----------------------------------------
    def set_eq(self, ch: int, band: int, hz=None, gain=None, q=None, on=None) -> None:
        if ch not in self.eq:
            return
        with self._lock:
            b = self.eq[ch].get(int(band))
            if b is None:
                return
            try:
                if hz is not None:
                    b["hz"] = _clampf(_num(hz), 20.0, 20000.0)
                if gain is not None:
                    b["gain"] = _clampf(_num(gain), -15.0, 15.0)
                if q is not None:
                    b["q"] = _clampf(_num(q), 0.3, 10.0)
                if on is not None:
                    b["on"] = bool(on)
            except (TypeError, ValueError):
                return
            self.con.set_eq_band(ch, int(band), b["hz"], b["gain"], b["q"], b["on"])
            self.assistant._emit("manual",
                                 f"{C.CHANNELS[ch]} EQ b{band}: {b['hz']:.0f} Hz "
                                 f"{b['gain']:+.1f} dB Q{b['q']:.1f} "
                                 f"{'on' if b['on'] else 'off'}", ch)
            self._rebuild_telemetry()

    def reset_channel_eq(self, ch: int) -> None:
        if ch not in self.eq:
            return
        with self._lock:
            for bnum, band in self.eq[ch].items():
                band["gain"], band["on"] = 0.0, False
                self.con.set_eq_band(ch, bnum, band["hz"], 0.0, band["q"], False)
            self.con.clear_channel_eq(ch)
            # clear_channel_eq also drops any parked feedback notch on this channel,
            # so resync the detector's state or it stays stuck "notched" and the
            # next notch lands on the wrong rotation band.
            if ch in self.assistant.used_notch_band:
                self.assistant.used_notch_band[ch] = 0
                self.assistant.fb_streak[ch] = 0
            self.assistant._emit("manual", f"{C.CHANNELS[ch]} EQ reset to flat", ch)
            self._rebuild_telemetry()

    def set_send(self, ch: int, fx: int, db: float) -> None:
        if ch not in self.sends or int(fx) not in C.FX_BUSES:
            return
        with self._lock:
            try:
                db = _clampf(_num(db), -90.0, 10.0)
            except (TypeError, ValueError):
                return
            self.sends[ch][int(fx)] = db
            self.con.set_send(ch, int(fx), db)
            self.assistant._emit("manual",
                                 f"{C.CHANNELS[ch]} → {C.FX_BUSES[int(fx)]} {db:+.0f} dB", ch)
            self._rebuild_telemetry()

    def set_fx_wet(self, fx: int, db: float) -> None:
        if int(fx) not in C.FX_BUSES:
            return
        with self._lock:
            try:
                db = _clampf(_num(db), -90.0, 10.0)
            except (TypeError, ValueError):
                return
            self.fx_wet[int(fx)] = db
            self.con.set_fx_return(int(fx), db)
            self.assistant._emit("manual", f"{C.FX_BUSES[int(fx)]} return {db:+.0f} dB")
            self._rebuild_telemetry()

    def set_mute(self, ch: int, on: bool) -> None:
        if ch not in C.CHANNELS:
            return
        with self._lock:
            self.con.set_channel_mute(ch, bool(on))
            self.muted[ch] = bool(on)
            # mark operator-touched so an automatic guest-mute yields to this
            self.assistant.note_manual(ch, time.monotonic())
            self.assistant._emit("manual",
                                 f"{C.CHANNELS[ch]} {'MUTED' if on else 'unmuted'}", ch)
            self._rebuild_telemetry()

    def set_master(self, db: float) -> None:
        try:
            db = _num(db)
        except (TypeError, ValueError):
            return
        with self._lock:
            self.master_db = self.con.set_main_fader(db)
            self.assistant._emit("manual", f"master fader → {self.master_db:+.1f} dB")
            self._rebuild_telemetry()

    def capture_balance_now(self) -> None:
        with self._lock:
            self.assistant.capture_balance(self._output_levels())
            self._rebuild_telemetry()

    # -- console -> app reconciliation (shared control) ---------------------
    def reconcile_fader(self, ch: int, db: float) -> None:
        """The console reported channel `ch` at `db` (e.g. a human moved a fader
        on the desk/iPad). If it disagrees with what we believe — and it wasn't
        just our own move (or its ramp) echoing back — adopt it and let auto-ride
        yield. A continuous human fader sweep is coalesced to one log line/0.5 s."""
        if ch not in C.CHANNELS:
            return
        with self._lock:
            now = time.monotonic()
            self._last_rx_t = now            # we heard from the console
            if now - self.assistant.last_move_t.get(ch, -1e9) < 0.8:
                return                       # echo of our own recent move / ramp — ignore
            if abs(db - self.assistant.fader_db.get(ch, 0.0)) < 0.5:
                return                       # already in sync
            self.assistant.fader_db[ch] = db
            self.assistant.note_manual(ch, now)
            if now - self._last_reconcile_log.get(ch, -1e9) > 0.5:
                self._last_reconcile_log[ch] = now
                self.assistant._emit("manual",
                                     f"{C.CHANNELS[ch]} moved on console → {db:+.1f} dB", ch)
            self._rebuild_telemetry()

    # -- show clock -> automatic scene recall -------------------------------
    def on_song_change(self, state) -> None:
        """A show clock (AbleSet or the simulator) reports the current song.
        Recall the template's scene for it and apply that song's reference levels
        — exactly once per song (section/bpm updates for the same song don't
        re-recall). Skips the recall during a takeover so we never fight the op."""
        with self._lock:
            self.show = {"current": state.song_name, "next": state.next_song,
                         "section": state.section, "bpm": state.bpm,
                         "playing": state.playing, "index": state.song_index,
                         "scene": None}
            song = self.template.song_by_name(state.song_name)
            # Dedup on the NORMALISED name (same basis as song_by_name) so a casing/
            # whitespace variant of the same song doesn't re-recall and stomp the op.
            norm = (state.song_name or "").strip().lower()
            same_song = (norm == self._applied_song)
            if song is not None:
                self.show["scene"] = song.scene
            if same_song:
                self._rebuild_telemetry()       # just a section/bpm/playing update
                return
            self._applied_song = norm
            self._song_start_t = time.monotonic()   # reset the 8-bar alert gate
            if song is None:
                self.assistant._emit("show", f"song: {state.song_name or '-'} "
                                             f"(no template entry — leaving mix as-is)")
                self._rebuild_telemetry()
                return
            coach = self.assistant.coach_mode
            if song.scene is not None and self.status != "takeover":
                if coach:                        # coach: advise the recall, don't do it
                    self.assistant._recommend("scene", None,
                        f"Song '{song.name}' → recall scene {song.scene} on the console.",
                        persist=True, scene=song.scene, song=song.name)
                else:
                    self._safe_recall(song.scene)
            if song.lead_target is not None:     # a target, not a console write — always ok
                self.assistant.lead_target = song.lead_target
            if song.balance:
                self.assistant.balance_targets.update(dict(song.balance))
            now2 = time.monotonic()
            for gch in C.GUEST_CHANNELS:          # (un)mute guest channels per song
                if gch not in C.CHANNELS or self.status == "takeover":
                    continue
                want = not song.guest
                if self.muted.get(gch) == want:               # already there — don't fight
                    continue
                if now2 < self.assistant.manual_hold_until.get(gch, -1e9):
                    continue                                   # operator just set it — yield
                if coach:                        # coach: advise the mute change, don't do it
                    self.assistant._recommend("guest", gch,
                        f"{'Mute' if want else 'Unmute'} {C.CHANNELS[gch]} for "
                        f"'{song.name}'.", persist=True, mute=want)
                else:
                    self.con.set_channel_mute(gch, want)
                self.muted[gch] = want           # believed state (optimistic in coach)
            bits = []
            if song.scene is not None:
                bits.append(f"scene {song.scene}")
            if song.lead_target is not None:
                bits.append(f"lead {song.lead_target:+.1f} dB")
            if song.guest:
                bits.append("guest")
            self.assistant._emit("show", f"song: {song.name} -> " +
                                 (", ".join(bits) if bits else "no changes"))
            self._rebuild_telemetry()

    def recall_scene_manual(self, index) -> None:
        """Operator tapped a scene in the Scenes panel — recall it directly."""
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return
        with self._lock:
            if self.status == "takeover":       # operator owns the desk — don't clobber it
                self.assistant._emit("show", "scene recall blocked — takeover engaged")
                self._rebuild_telemetry()
                return
            self._safe_recall(idx)
            self.show["scene"] = idx
            self.assistant._emit("show", f"scene {idx} recalled (manual)")
            self._rebuild_telemetry()

    def _safe_recall(self, idx: int) -> None:
        """Recall a scene, tolerating a console socket torn down during shutdown
        (a late show-clock straggler must never raise on a closed transport)."""
        try:
            self.con.recall_scene(idx)
        except OSError:
            pass

    # -- venue learning (a prior, never an action) --------------------------
    def apply_venue_model(self, model) -> None:
        """Seed the assistant's watch-list with a room's known feedback freqs so
        the (guard-railed) detector reacts a block sooner. Never applies cuts."""
        if model is None:
            return
        with self._lock:
            self.venue_model = model
            self.assistant.venue_watch_freqs = model.watch_freqs()[:12]   # bound (per-block scan)
            self.assistant._emit("system",
                                 f"venue model loaded: {model.shows} show(s), "
                                 f"{len(self.assistant.venue_watch_freqs)} known feedback "
                                 f"freq(s), confidence {model.confidence:.0%}")
            self._rebuild_telemetry()

    def learn_venue(self):
        """Post-show: mine the session log and (re)write this venue's model."""
        if self.session_log is None or not self.venue or not self.venue_dir:
            return None
        try:
            from . import venue as venuemod
            self.session_log.flush()
            model = venuemod.build_model(self.session_log, self.venue, now=time.time())
            path = venuemod.save_model(model, self.venue_dir)
            self.venue_model = model
            return path
        except Exception:
            return None

    # -- AI slow layer (advisory only; never controls anything) -------------
    def advisor_context(self) -> dict:
        """A compact snapshot for the Claude advisor. Taken under the lock, then
        the (slow) network call happens off-lock in the Advisor thread."""
        with self._lock:
            recent = [{"kind": e["kind"], "level": e.get("level"), "msg": e["msg"]}
                      for e in list(self.events)[-25:]]
            chans = []
            for ch, role in C.CHANNELS.items():
                feat = self._feat.get(ch)
                chans.append({"role": role,
                              "rms_db": round(feat.rms_dbfs, 1) if feat else None,
                              "notched": bool(self.assistant.used_notch_band.get(ch))})
            return {
                "mode": "simulation" if self.sim else "hardware",
                "show": dict(self.show),
                "alerts": list(self.telemetry.get("alerts", [])),
                "lead": dict(self.telemetry.get("lead", {})),
                "channels": chans,
                "recent_events": recent,
            }

    def on_advice(self, advice: dict) -> None:
        """Receive a note from the advisor: log it + surface it in telemetry. The
        advice is ADVISORY text only — no control action is taken from it."""
        with self._lock:
            prompt = str(advice.get("performer_prompt", "")).strip()[:200]
            sev = str(advice.get("severity", "info")).strip().lower()
            if sev not in ("info", "notice", "warn"):
                sev = "info"
            self.advisor_advice = {
                "prompt": prompt,
                "assessment": str(advice.get("room_assessment", "")).strip()[:240],
                "severity": sev,
                "time": time.strftime("%H:%M:%S"),
            }
            if prompt:
                self.assistant._emit("advisor", prompt)
            self._rebuild_telemetry()

    def set_console_meters(self, vals: list[float]) -> None:
        # Only the count is used in telemetry; storing an int (atomic rebind) avoids
        # a cross-thread list and keeps the "all state behind the lock" invariant
        # honest (no list to race on).
        self._meter_count = len(vals)
        self._last_rx_t = time.monotonic()

    def command(self, d: dict) -> None:
        """Dispatch a control message (WebSocket / POST). Tolerant of malformed
        or hostile input — a bad message is ignored, never crashes a thread."""
        if not isinstance(d, dict):
            return
        t = d.get("type")
        try:
            if t == "fader":
                self.set_fader(int(d["ch"]), _num(d["db"]))
            elif t == "mute":
                self.set_mute(int(d["ch"]), bool(d["on"]))
            elif t == "master":
                self.set_master(_num(d["db"]))
            elif t == "toggle":
                self.set_enabled(str(d["job"]), bool(d["on"]))
            elif t == "coach":
                self.set_coach_mode(bool(d["on"]))
            elif t == "panic":
                self.panic(bool(d["on"]))
            elif t == "calibrate":
                self.run_calibration()
            elif t == "lead_target":
                self.set_lead_target(_num(d["db"]))
            elif t == "balance_capture":
                self.capture_balance_now()
            elif t == "reset_notches":
                self.reset_notches()
            elif t == "scene":
                self.recall_scene_manual(d["index"])
            elif t == "eq":
                self.set_eq(int(d["ch"]), int(d["band"]), d.get("hz"),
                            d.get("gain"), d.get("q"), d.get("on"))
            elif t == "eq_reset":
                self.reset_channel_eq(int(d["ch"]))
            elif t == "send":
                self.set_send(int(d["ch"]), int(d["fx"]), _num(d["db"]))
            elif t == "fx":
                self.set_fx_wet(int(d["fx"]), _num(d["db"]))
        except (KeyError, TypeError, ValueError):
            pass

    def reset_notches(self) -> None:
        with self._lock:
            self.assistant.reset_notches()
            self._rebuild_telemetry()

    def panic(self, on: bool) -> None:
        with self._lock:
            if on and self.status != "takeover":
                self._enabled_backup = dict(self.assistant.enabled)
                for k in self.assistant.enabled:
                    self.assistant.enabled[k] = False
                self.con.set_main_mute(True)
                self.status = "takeover"
                self.assistant._emit("panic", "TAKEOVER — main muted, all jobs held")
            elif not on and self.status == "takeover":
                self.con.set_main_mute(False)
                if self._enabled_backup is not None:
                    self.assistant.enabled.update(self._enabled_backup)
                self.status = "live"
                self.assistant._emit("panic", "takeover released — back to live")
            self._rebuild_telemetry()

    # -- calibration --------------------------------------------------------
    def run_calibration(self) -> None:
        with self._lock:
            # never calibrate during a takeover (it would actuate the bus EQ),
            # and never double-spawn a worker.
            if self.calib_status == "running" or self.status == "takeover":
                return
            self.calib_status = "running"
            self.status = "calibrating"
            self.assistant._emit("calibration", "pink-noise sweep — emitting & capturing…")
            self._rebuild_telemetry()
        threading.Thread(target=self._calibrate_worker, daemon=True).start()

    def _calibrate_worker(self) -> None:
        # Emit pink noise out the PA and record it at the meas mic, then analyse.
        # Both the recording and the heavy FFT analysis run OFF the lock so they
        # can't freeze the engine loop or any client feed; only the fast OSC
        # apply happens under the lock. (SimCapture returns a synthetic capture
        # instantly; SoundDeviceCapture plays + records for real.)
        time.sleep(1.2)                                  # let the UI register the state
        capture = None
        played = True
        if self.source is not None:
            played = self.source.emit_pink_noise(dsp.generate_pink_noise(C.CAL_DURATION_S))
            capture = self.source.capture_meas(C.CAL_DURATION_S)   # ~instant in sim, real on hw
        res, silent, level_db = None, False, None
        if capture is not None and capture.size:
            rms = float(np.sqrt(np.mean(np.square(capture, dtype=np.float64))))
            level_db = 20.0 * math.log10(rms + 1e-9)
            if rms < 10 ** (-50.0 / 20.0):               # heard ~nothing (dead PA/mic)
                silent = True
            else:
                res = self.calibrator.analyze(capture)
        with self._lock:
            if self.status == "takeover":                # operator grabbed control mid-sweep
                self.calib_status = "none"
                self.assistant._emit("calibration", "calibration aborted — takeover engaged")
                self._rebuild_telemetry()
                return
            if silent:                                   # don't "correct" against silence
                self.calib_status = "none"
                if self.status == "calibrating":
                    self.status = "live"
                hint = ("the test tone could not be played — check the output device"
                        if not played else
                        "raise the mic gain / speaker level or move the mic closer")
                lvl = f"{level_db:.0f} dBFS" if level_db is not None else "silence"
                self.assistant._emit("calibration",
                                     f"calibration heard only {lvl} at the mic "
                                     f"(needs > -50 dBFS) — {hint}")
                self._rebuild_telemetry()
                return
            if res is None:                              # no meas-mic capture available
                self.calib_status = "none"
                if self.status == "calibrating":
                    self.status = "live"                 # don't get stuck "calibrating"
                msg = ("couldn't read the measurement mic (no audio captured) — check "
                       "--audio-device and Windows microphone access"
                       if self.source is not None else
                       "no audio-capture source available")
                self.assistant._emit("calibration",
                                     "calibration needs a measurement-mic input — " + msg)
                self._rebuild_telemetry()
                return
            self.calib = res
            self.assistant.set_calibration(res)          # watch-list is detection state, not console
            if self.assistant.coach_mode:
                # Coach mode: measure the room but ADVISE the main-bus EQ instead of
                # writing it — coach mode's promise is that the console is untouched.
                self.assistant.coach_calibration(self.calibrator.plan(res))
                self.assistant._emit("calibration",
                                     f"coach: measured room — {len(res.corrections)} cut(s), "
                                     f"{len(res.watch_freqs)} watch freq(s); advising manual "
                                     "main-bus EQ (console untouched)")
            else:
                used = self.calibrator.apply(
                    res, log=lambda m: self.assistant._emit("calibration", m))
                self.assistant.cal_bus_bands = used      # feedback notching avoids these
                # apply() flattened the whole bus, so any prior feedback notches are
                # gone — reset the feedback-band tracking to match.
                self.assistant.fb_bus_bands = []
                self.assistant.used_bus_notch_band = 0
                self.assistant._emit("calibration",
                                     f"done — {len(res.corrections)} room cut(s), "
                                     f"{len(res.watch_freqs)} feedback-prone freq(s) on watch-list")
            self.calib_status = "done"
            if self.status == "calibrating":
                self.status = "live"
            self._rebuild_telemetry()

    # -- helpers ------------------------------------------------------------
    def _output_levels(self) -> dict[int, float]:
        return {ch: f.rms_dbfs for ch, f in self._feat.items()}

    def _channel_telemetry(self, now: float) -> list[dict]:
        out = []
        for ch, role in C.CHANNELS.items():
            feat = self._feat.get(ch)
            rms = feat.rms_dbfs if feat else -90.0
            peak = feat.peak_dbfs if feat else -90.0
            streak = self.assistant.fb_streak.get(ch, 0)
            notched_recent = now - self._last_notch_t[ch] < 2.0
            clip_recent = now - self._last_clip_t[ch] < 1.5
            if notched_recent:
                fb_state = "notched"
            elif streak >= 2:                 # sustained, not a single-block blip
                fb_state = "building"
            else:
                fb_state = "ok"
            notch_freqs = (self.con.notch_freqs(ch)
                           if isinstance(self.con, SimConsole) else [])
            out.append({
                "ch": ch, "role": role, "label": C.ROLE_LABELS[role],
                "rms": round(rms, 1), "peak": round(peak, 1),
                "fader": round(self.assistant.fader_db[ch], 1),
                "gain": round(self.assistant.gain_db[ch], 1),
                "fb_state": fb_state, "fb_streak": streak,
                "notch_freqs": [round(f) for f in notch_freqs],
                "clip_recent": clip_recent,
                "gain_trimmed": self.assistant.gain_db[ch] < self.assistant.nominal_gain[ch] - 0.1,
                "muted": self.muted.get(ch, False),
                "manual_hold": now < self.assistant.manual_hold_until.get(ch, -1e9),
                "is_meas": ch == C.MEAS_MIC_CH,
                "is_lead": ch == C.LEAD_VOCAL_CH,
                "is_balance": ch in C.BALANCE_CHANNELS,
                "in_mix": ch != C.MEAS_MIC_CH,
            })
        return out

    OPENING_BARS = 8        # suppress non-critical alert pop-ups for this many bars
    DEAD_CRITICAL_S = 1.5   # audio must be dead this long before the critical overlay

    def _in_opening_bars(self, now: float) -> bool:
        if not self.show.get("playing") or self._song_start_t < 0:
            return False
        bpm = self.show.get("bpm")
        if isinstance(bpm, (int, float)) and bpm > 0:
            sec_per_bar = 4.0 * 60.0 / bpm          # assume 4/4
            return (now - self._song_start_t) < self.OPENING_BARS * sec_per_bar
        return (now - self._song_start_t) < 16.0    # no BPM -> ~16 s fallback

    def _alerts_and_mode(self, now: float, connected: bool) -> tuple[list, str]:
        """Compute the operator-facing alerts and the AUTO/ALERT/MANUAL headline."""
        alerts = []
        cap = self.source.status() if self.source is not None else {"kind": "none"}
        dead = cap.get("dead")
        if dead and self._dead_since < 0:
            self._dead_since = now                 # mark when the dropout began
        elif not dead:
            self._dead_since = -1e9
        if dead and (now - self._dead_since) > self.DEAD_CRITICAL_S:
            # sustained -> the interface is really gone: level-4 critical overlay
            alerts.append({"level": "critical",
                           "msg": "audio capture stopped — the assistant is deaf "
                                  "(check the interface is connected)"})
        elif dead:
            # a brief dropout (recovering) shouldn't pop the full-screen overlay
            alerts.append({"level": "warn", "msg": "audio dropout — recovering…"})
        elif cap.get("silent"):
            # delivering frames but they're digital silence — the classic macOS
            # "Microphone access denied" symptom (also: muted input / zero gain).
            alerts.append({"level": "warn",
                           "msg": "inputs are silent — check Microphone permission "
                                  "(macOS), input gain, and the audio patch"})
        elif cap.get("overload"):
            alerts.append({"level": "warn",
                           "msg": "audio interface is dropping samples (xruns) — "
                                  "raise the device buffer size"})
        if (not self.sim) and self.meter_rx is not None and not connected:
            ip = getattr(self.con, "ip", None)
            where = f" from {ip}" if ip else ""
            alerts.append({"level": "warn",
                           "msg": f"no console feed{where} — check the console IP, "
                                  "network, and firewall"})
        if (not self.sim) and self.source is None:
            alerts.append({"level": "notice",
                           "msg": "manual control only (no audio device selected)"})
        # Performer comfort (spec p.10): no non-critical alert pop-ups during the
        # first 8 bars of a song. Auto-corrections still happen + everything is
        # still logged; only the banner/overlay is held. Safety-critical stays.
        if self._in_opening_bars(now):
            alerts = [a for a in alerts if a["level"] == "critical"]
        if self.status == "takeover":
            op_mode = "manual"
        elif self.status == "calibrating":
            op_mode = "calibrating"
        elif any(a["level"] in ("warn", "critical") for a in alerts):
            op_mode = "alert"          # safety alerts still win the pill over coach
        elif self.assistant.coach_mode:
            op_mode = "coach"
        else:
            op_mode = "auto"
        return alerts, op_mode

    def _scene_list(self) -> list:
        cur = getattr(self.con, "last_scene", None)
        out, seen = [], set()
        for sg in self.template.songs:
            if sg.scene is not None and sg.scene not in seen:
                seen.add(sg.scene)
                out.append({"index": sg.scene, "name": sg.name, "current": sg.scene == cur})
        return out

    def _rebuild_telemetry(self) -> None:
        now = time.monotonic()
        lead = C.LEAD_VOCAL_CH
        in_ema = self.assistant.in_ema.get(lead)
        lead_out = (in_ema + self.assistant.fader_db[lead]) if in_ema is not None else None
        connected = (now - self._last_rx_t) < 5.0
        alerts, op_mode = self._alerts_and_mode(now, connected)
        tele = {
            "status": self.status,
            "op_mode": op_mode,                    # auto | alert | manual | calibrating
            "alerts": alerts,
            "scenes": self._scene_list(),
            "mode": "simulation" if self.sim else "live-hardware",
            "capture": self.source.status() if self.source is not None else {"kind": "none"},
            "console": {"meters": self._meter_count,
                        "connected": connected},   # heard recently?
            "uptime": round(now - self._t0, 1),
            "perf": dict(self._perf),              # tick compute / jitter / max (ms)
            "latency": {k: dict(v) for k, v in self._latency.items()},  # detect→actuate ms
            "block_ms": round(C.BLOCK / C.SAMPLE_RATE * 1000.0, 1),     # inherent buffer latency
            "room": {"level_db": round(self.assistant.room_level_db, 1),
                     "contrast_db": round(self.assistant.room_contrast_db, 1),
                     "confidence": self.assistant.room_confidence},
            "enabled": dict(self.assistant.enabled),
            "coach": {"on": self.assistant.coach_mode,
                      "recs": self.assistant.coach_snapshot(now)},
            "main_muted": getattr(self.con, "main_muted", False),
            "master": {"fader": round(self.master_db, 1)},
            "eq": {c: [{"band": b, "hz": round(bd["hz"]), "gain": round(bd["gain"], 1),
                        "q": round(bd["q"], 1), "on": bd["on"]}
                       for b, bd in sorted(self.eq[c].items())] for c in self.eq},
            "sends": {c: {fx: round(v, 1) for fx, v in self.sends[c].items()}
                      for c in self.sends},
            "fx": {"buses": {str(k): v for k, v in C.FX_BUSES.items()},
                   "wet": {str(k): round(v, 1) for k, v in self.fx_wet.items()}},
            "show": dict(self.show),
            "advisor": dict(self.advisor_advice) if self.advisor_advice else None,
            "advisor_on": self.advisor is not None and getattr(self.advisor, "available", False),
            "venue": ({"name": self.venue, "shows": self.venue_model.shows,
                       "confidence": self.venue_model.confidence,
                       "freqs": [f["hz"] for f in self.venue_model.feedback_freqs]}
                      if self.venue_model else None),
            "lead": {
                "target": round(self.assistant.lead_target, 1),
                "input": round(in_ema, 1) if in_ema is not None else None,
                "output": round(lead_out, 1) if lead_out is not None else None,
            },
            "balance_targets": {c: round(v, 1)
                                for c, v in self.assistant.balance_targets.items()},
            "calibration": {
                "status": self.calib_status,
                "baseline": ([{"hz": k, "db": round(v, 1)}
                              for k, v in self.calib.baseline.items()]
                             if self.calib else []),
                "watchlist": ([{"hz": round(f), "excess": e}
                               for f, e in self.calib.watchlist] if self.calib else []),
                "corrections": ([{"hz": round(f), "cut": c}
                                 for f, c in self.calib.corrections] if self.calib else []),
            },
            "channels": self._channel_telemetry(now),
            "log": list(self.events)[-40:][::-1],
        }
        # Caller already holds self._lock (every call site is inside a locked
        # section), so building from shared state and the deque is race-free.
        self.telemetry = tele

    def snapshot(self) -> dict:
        with self._lock:
            return self.telemetry
