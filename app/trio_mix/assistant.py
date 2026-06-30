"""The decision brain: four guard-railed jobs + a typed decision log.

Tiers (by reaction time):
  * reflex  — feedback notch, clip trim         (act within a few blocks)
  * advisory— lead-vocal ride, balance hold      (act over ~0.5 s windows)

Every move goes through the console's guardrail clamps; sizes are additionally
limited here (MAX_STEP). Every decision is logged and reversible.
"""
from __future__ import annotations

import time
from collections import deque

from . import config as C
from .calibration import CalibrationResult
from .osc import ConsoleBase


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class MixAssistant:
    def __init__(self, console: ConsoleBase,
                 calib: CalibrationResult | None = None,
                 on_event=None, on_latency=None) -> None:
        self.con = console
        self.on_event = on_event
        self.on_latency = on_latency               # (kind, seconds) per actuation
        self.block_t0 = 0.0                         # perf_counter when this block's analysis began
        self.enabled = dict(C.DEFAULT_ENABLED)
        self.lead_target = C.LEAD_TARGET_DB        # runtime-adjustable

        chans = list(C.CHANNELS)
        self.fader_db = {ch: 0.0 for ch in chans}        # believed positions
        self.gain_db = {ch: 20.0 for ch in chans}
        self.nominal_gain = {ch: 20.0 for ch in chans}

        # feedback state
        self.fb_streak = {ch: 0 for ch in chans}
        self.used_notch_band = {ch: 0 for ch in chans}
        self.used_bus_notch_band = 0          # rotation counter for room-feedback bands
        self.fb_bus_bands: list[int] = []     # bus bands we've parked feedback cuts on
        self.cal_bus_bands: set[int] = set()  # bus bands calibration owns (don't clobber)
        self.fb_last_freq = {ch: None for ch in chans}
        self.fb_last_level = {ch: -90.0 for ch in chans}

        # clip recovery
        self.last_clip_t = {ch: -1e9 for ch in chans}
        self.last_recover_t = {ch: -1e9 for ch in chans}

        # level rides (input EMAs + last-move gates)
        self.in_ema = {ch: None for ch in chans}
        self.last_ride_t = {ch: -1e9 for ch in chans}
        self.last_move_t = {ch: -1e9 for ch in chans}         # when WE last moved a fader
        self.stage_baseline = None                            # slow stage-loudness baseline
        self.last_stage_warn = -1e9
        # room-mic SNR / confidence (loud crowd -> less reliable feedback detection)
        self.room_level_db = -90.0
        self.room_contrast_db = 0.0
        self.room_confidence = 1.0
        self.last_lowconf_warn = -1e9
        self.manual_hold_until = {ch: -1e9 for ch in chans}   # human override window
        self.balance_targets: dict[int, float] = {}      # ch -> target output dB

        # calibration products
        self.set_calibration(calib)
        self.venue_watch_freqs: list = []      # venue-learned freqs (persist across recal)

        self.log: deque = deque(maxlen=200)

    # -- calibration --------------------------------------------------------
    def set_calibration(self, calib: CalibrationResult | None) -> None:
        self.calib = calib
        self.watch_freqs = list(calib.watch_freqs) if calib else []

    # -- logging ------------------------------------------------------------
    def _emit(self, kind: str, msg: str, ch: int | None = None) -> None:
        ev = {"time": time.strftime("%H:%M:%S"), "kind": kind,
              "ch": ch, "role": C.CHANNELS.get(ch), "msg": msg}
        self.log.append(ev)
        if self.on_event:
            self.on_event(ev)

    def _note_latency(self, kind: str) -> None:
        """Record detect→actuate latency: from when this block's analysis began
        (engine sets block_t0) to the OSC command we just issued."""
        if self.on_latency:
            self.on_latency(kind, time.perf_counter() - self.block_t0)

    def _is_near_watch(self, freq: float, tol: float = 0.06) -> bool:
        return any(abs(freq - w) / w < tol
                   for w in (self.watch_freqs + self.venue_watch_freqs) if w)

    def _alloc_bus_band(self) -> int:
        """Pick a main-bus EQ band for a room-feedback notch that does NOT clobber
        a band calibration claimed. Prefer a free band; else rotate among bands we
        already own; only if all 6 are calibration-owned, take the last band."""
        for b in range(1, 7):
            if b not in self.cal_bus_bands and b not in self.fb_bus_bands:
                return b
        if self.fb_bus_bands:
            return self.fb_bus_bands[self.used_bus_notch_band % len(self.fb_bus_bands)]
        return 6

    # ======================================================================
    # Reflex 1 — feedback catch & notch (calibration-aware)
    # ======================================================================
    def handle_feedback(self, ch: int, feat) -> None:
        if not self.enabled["feedback"]:
            return
        if feat.fb_freq:
            prev = self.fb_last_freq[ch]
            stable = (prev is not None
                      and abs(feat.fb_freq - prev) / feat.fb_freq < C.FB_STABLE_TOL)
            rising = feat.rms_dbfs > self.fb_last_level[ch] + C.FB_RISE_DB
            near = self._is_near_watch(feat.fb_freq)
            need = max(1, C.FB_SUSTAIN_BLOCKS - (1 if near else 0))
            if ch == C.MEAS_MIC_CH and self.room_confidence < 0.7:
                # loud/noisy room: require more sustain before a room-feedback
                # notch, so crowd noise can't trigger a false main-bus cut
                need = max(need, int(round(need / max(0.3, self.room_confidence))))

            if stable and rising:
                self.fb_streak[ch] += 1
            else:
                self.fb_streak[ch] = max(0, self.fb_streak[ch] - 1)

            if self.fb_streak[ch] >= need:
                if ch == C.MEAS_MIC_CH:
                    # Acoustic feedback heard in the room -> notch the MAIN BUS,
                    # which cuts the frequency from everything feeding the PA.
                    # The meas mic isn't in the mix, so notching its own channel
                    # EQ would do nothing. A band allocator keeps these clear of
                    # the bands calibration claimed (no clobbering room cuts).
                    band = self._alloc_bus_band()
                    self.con.set_bus_eq(band, feat.fb_freq,
                                        C.FB_NOTCH_GAIN_DB, C.FB_NOTCH_Q)
                    if band not in self.fb_bus_bands:
                        self.fb_bus_bands.append(band)
                    self.used_bus_notch_band += 1
                    where = f"main-bus band {band}"
                else:
                    band = (self.used_notch_band[ch] % 4) + 1
                    self.con.set_eq_notch(ch, band, feat.fb_freq)
                    self.used_notch_band[ch] = band
                    where = f"notch band {band}"
                flag = " [watch-list]" if near else ""
                self._emit("feedback",
                           f"{feat.fb_freq:.0f} Hz on {C.CHANNELS[ch]} → {where}{flag}", ch)
                self._note_latency("feedback")
                self.fb_streak[ch] = 0
            self.fb_last_freq[ch] = feat.fb_freq
            self.fb_last_level[ch] = feat.rms_dbfs
        else:
            self.fb_streak[ch] = max(0, self.fb_streak[ch] - 1)
            self.fb_last_freq[ch] = None
            self.fb_last_level[ch] = feat.rms_dbfs

    # ======================================================================
    # Reflex 2 — clip / overload protection (with slow recovery)
    # ======================================================================
    def handle_clip(self, ch: int, feat, now: float) -> None:
        if not self.enabled["clip"]:
            return
        if feat.peak_dbfs > C.CLIP_PEAK_DBFS:
            self.gain_db[ch] = self.con.nudge_gain_db(
                ch, self.gain_db[ch], C.CLIP_TRIM_DB)
            self.last_clip_t[ch] = now
            self._emit("clip",
                       f"{C.CHANNELS[ch]} near clip → preamp "
                       f"{C.CLIP_TRIM_DB:+.0f} dB → {self.gain_db[ch]:.0f} dB", ch)
            self._note_latency("clip")
        elif (feat.peak_dbfs < C.CLIP_PEAK_DBFS - 6.0
              and self.gain_db[ch] < self.nominal_gain[ch]
              and now - self.last_clip_t[ch] > C.CLIP_RECOVER_S
              and now - self.last_recover_t[ch] > C.CLIP_RECOVER_S):
            target = min(self.nominal_gain[ch],
                         self.gain_db[ch] + C.CLIP_RECOVER_DB)
            self.gain_db[ch] = self.con.nudge_gain_db(
                ch, self.gain_db[ch], target - self.gain_db[ch])
            self.last_recover_t[ch] = now
            self._emit("clip",
                       f"{C.CHANNELS[ch]} clean → restoring preamp "
                       f"→ {self.gain_db[ch]:.0f} dB", ch)

    # ======================================================================
    # Advisory — a clamped output-leveler shared by vocal-ride & balance
    # ======================================================================
    def _ride_toward(self, ch: int, feat, target_out: float, now: float,
                     min_interval: float, kind: str) -> None:
        a = 1 / 24.0
        e = self.in_ema[ch]
        self.in_ema[ch] = feat.rms_dbfs if e is None else (1 - a) * e + a * feat.rms_dbfs
        if now < self.manual_hold_until.get(ch, -1e9):
            return                          # a human just moved this fader — yield
        if now - self.last_ride_t[ch] < min_interval:
            return
        output_est = self.in_ema[ch] + self.fader_db[ch]
        err = target_out - output_est
        if abs(err) <= C.LEAD_TOLERANCE:
            return
        step = _clamp(err, -C.MAX_STEP_DB, C.MAX_STEP_DB)
        start = self.fader_db[ch]
        newf = _clamp(start + step, C.FADER_MIN_DB, C.FADER_MAX_DB)
        self.last_ride_t[ch] = now
        if abs(newf - start) < 0.05:        # railed / nothing to do -> don't spam
            return
        self.fader_db[ch] = self.con.ramp_fader_db(ch, start, newf)
        self.last_move_t[ch] = now            # so reconciliation ignores our echo
        self._note_latency(kind)
        self._emit(kind,
                   f"{C.CHANNELS[ch]}: out {output_est:+.1f} dB vs target "
                   f"{target_out:+.1f} → fader {start:+.1f}→{self.fader_db[ch]:+.1f} dB",
                   ch)

    def handle_vocal_ride(self, feat, now: float) -> None:
        if not self.enabled["vocal_ride"]:
            return
        self._ride_toward(C.LEAD_VOCAL_CH, feat, self.lead_target, now,
                          min_interval=0.25, kind="vocal")

    def handle_balance(self, ch: int, feat, now: float) -> None:
        if not self.enabled["balance"] or ch not in self.balance_targets:
            return
        self._ride_toward(ch, feat, self.balance_targets[ch], now,
                          min_interval=1.0, kind="balance")

    def note_manual(self, ch: int, now: float, hold: float = 5.0) -> None:
        """Mark a channel as just-touched-by-a-human; auto yields for `hold` s."""
        self.manual_hold_until[ch] = now + hold

    def capture_balance(self, levels: dict[int, float]) -> None:
        """Snapshot the balance channels' current output as hold targets, using
        the same smoothed (EMA) level basis the ride uses — so capturing doesn't
        cause an immediate spurious correction from EMA-vs-instantaneous lag."""
        self.balance_targets = {}
        for ch in C.BALANCE_CHANNELS:
            lvl = self.in_ema.get(ch)
            if lvl is None:
                lvl = levels.get(ch)
            if lvl is not None:
                self.balance_targets[ch] = lvl + self.fader_db[ch]
        pretty = ", ".join(f"{C.CHANNELS[c]} {v:+.1f}"
                           for c, v in self.balance_targets.items())
        self._emit("balance", f"captured balance targets: {pretty}")

    # ======================================================================
    # Reversibility
    # ======================================================================
    def reset_notches(self) -> None:
        cleared = 0
        for ch in C.CHANNELS:
            if self.used_notch_band[ch]:
                self.con.clear_channel_eq(ch)
                self.used_notch_band[ch] = 0
                self.fb_streak[ch] = 0
                cleared += 1
        for band in self.fb_bus_bands:               # flatten parked main-bus feedback cuts
            self.con.set_bus_eq(band, 1000.0, 0.0, q=2.0)
            cleared += 1
        self.fb_bus_bands = []
        self.used_bus_notch_band = 0
        self._emit("system", f"cleared {cleared} parked notch(es)")

    # ======================================================================
    # Advisory — stage-ambient mic: sense a sustained rise in stage loudness
    # (musicians playing harder), so the operator gets a heads-up. Opt-in
    # (C.STAGE_MIC_CH); never moves anything.
    # ======================================================================
    def handle_stage_volume(self, feat, now: float) -> None:
        lvl = feat.rms_dbfs
        if self.stage_baseline is None:
            self.stage_baseline = lvl
        self.stage_baseline = 0.999 * self.stage_baseline + 0.001 * lvl   # very slow
        if (lvl > self.stage_baseline + C.STAGE_RISE_DB
                and now - self.last_stage_warn > 30.0):
            self.last_stage_warn = now
            self._emit("system",
                       f"stage volume up ~{lvl - self.stage_baseline:.0f} dB over baseline "
                       "— consider easing back", C.STAGE_MIC_CH)

    # -- main dispatch ------------------------------------------------------
    @staticmethod
    def _room_conf(level_db: float, contrast_db: float) -> float:
        """0..1 confidence in the room mic: a loud, broadband (low-contrast) room
        masks feedback, so trust drops. Quiet/tonal -> ~1.0; loud+flat -> ~0.3."""
        loud = 1.0 if level_db <= -45.0 else (0.3 if level_db >= -15.0
               else 1.0 - 0.7 * (level_db + 45.0) / 30.0)
        # very flat spectrum (contrast < ring threshold) further reduces trust
        flat = 1.0 if contrast_db >= C.FB_RING_DB else max(0.5, contrast_db / C.FB_RING_DB)
        return round(loud * flat, 3)

    def _update_room(self, feat, now: float) -> None:
        a = 1 / 12.0
        self.room_level_db = (1 - a) * self.room_level_db + a * feat.rms_dbfs
        self.room_contrast_db = (1 - a) * self.room_contrast_db + a * feat.contrast_db
        self.room_confidence = self._room_conf(self.room_level_db, self.room_contrast_db)
        if self.room_confidence < 0.5 and now - self.last_lowconf_warn > 30.0:
            self.last_lowconf_warn = now
            self._emit("system",
                       f"room-mic SNR low (conf {self.room_confidence:.0%}) — "
                       "feedback detection is being more conservative", C.MEAS_MIC_CH)

    def on_features(self, ch: int, feat, now: float) -> None:
        if ch == C.MEAS_MIC_CH:
            # room truth: track SNR/confidence, then watch for acoustic feedback
            # only (never ride/clip it).
            self._update_room(feat, now)
            self.handle_feedback(ch, feat)
            return
        if C.STAGE_MIC_CH is not None and ch == C.STAGE_MIC_CH:
            self.handle_stage_volume(feat, now)
            return
        self.handle_feedback(ch, feat)
        self.handle_clip(ch, feat, now)
        if ch == C.LEAD_VOCAL_CH:
            self.handle_vocal_ride(feat, now)
        if ch in C.BALANCE_CHANNELS:
            self.handle_balance(ch, feat, now)

    # -- telemetry ----------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "enabled": dict(self.enabled),
            "fader_db": {c: round(v, 1) for c, v in self.fader_db.items()},
            "gain_db": {c: round(v, 1) for c, v in self.gain_db.items()},
            "notch_bands": {c: self.used_notch_band[c] for c in C.CHANNELS},
            "lead_target": self.lead_target,
            "balance_targets": {c: round(v, 1) for c, v in self.balance_targets.items()},
        }
