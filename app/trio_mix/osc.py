"""OSC console interface + X32/M32 wire-format scaling helpers.

ConsoleBase holds all the clamping + scaling logic and emits real OSC wire
values through ``_send``. Two concrete consoles:

  * OscConsole  — sends UDP to a real M32C (needs python-osc).
  * SimConsole  — records a semantic mirror of every move so the simulator and
                  tests can introspect state without any hardware.

Every high-level method also calls ``_record(kind, **semantic)`` so a console
can keep a clean, human-meaningful view of its state alongside the raw wire
traffic.
"""
from __future__ import annotations

import math
import queue
import threading
import time

from . import config as C


# ---------------------------------------------------------------------------
# Wire-format scaling (X32/M32). Faders/gains are LINEAR 0..1, never dB.
# ---------------------------------------------------------------------------
def db_to_fader(db: float) -> float:
    """dB -> X32 linear fader position 0.0..1.0 (always in range)."""
    if db <= -90.0:
        return 0.0
    if db < -60.0:
        f = (db + 90.0) / 480.0
    elif db < -30.0:
        f = (db + 70.0) / 160.0
    elif db < -10.0:
        f = (db + 50.0) / 80.0
    else:
        f = (db + 30.0) / 40.0          # -10..+10 dB
    return min(1.0, max(0.0, f))        # never emit out-of-range wire values


def fader_to_db(f: float) -> float:
    """Inverse of db_to_fader."""
    if f <= 0.0:
        return -90.0
    if f < 0.0625:
        return f * 480.0 - 90.0
    if f < 0.25:
        return f * 160.0 - 70.0
    if f < 0.5:
        return f * 80.0 - 50.0
    return f * 40.0 - 30.0


def freq_to_eq_param(hz: float) -> float:
    """X32 EQ frequency, log-scaled 0..1 over 20 Hz - 20 kHz.
    Input is clamped to the valid band so a degenerate/sub-20 Hz frequency can
    never produce log10(<=0) or an out-of-range parameter."""
    hz = max(20.0, min(20000.0, hz))
    return math.log10(hz / 20.0) / 3.0


def eq_param_to_freq(p: float) -> float:
    """Inverse of freq_to_eq_param."""
    return 20.0 * (10.0 ** (3.0 * p))


def q_to_param(q: float) -> float:
    """X32 Q, log-scaled 10..0.3 over 0..1 (inverted)."""
    q = max(0.3, min(10.0, q))
    return (math.log10(10.0) - math.log10(q)) / (math.log10(10.0) - math.log10(0.3))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Console base
# ---------------------------------------------------------------------------
class ConsoleBase:
    """Thin, guard-railed wrapper around the M32C parameter tree."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ramp_q: queue.Queue | None = None     # lazy async-ramp worker

    # -- transport hooks (overridden) ---------------------------------------
    def _send(self, addr: str, *args) -> None:
        raise NotImplementedError

    def _record(self, kind: str, **_semantic) -> None:
        """Optional semantic mirror for sim/tests. No-op by default; subclasses
        (e.g. SimConsole) override this to record the keyword fields."""

    def close(self) -> None:
        """Release transport resources; stop the ramp worker if it was started."""
        if self._ramp_q is not None:
            self._ramp_q.put(None)            # sentinel -> worker exits its loop

    # -- faders -------------------------------------------------------------
    def set_fader_db(self, ch: int, db: float) -> float:
        db = _clamp(db, C.FADER_MIN_DB, C.FADER_MAX_DB)
        with self._lock:
            self._send(f"/ch/{ch:02d}/mix/fader", db_to_fader(db))
        self._record("fader", ch=ch, db=db)
        return db

    def ramp_fader_db(self, ch: int, start_db: float, end_db: float,
                      ms: int = C.RAMP_MS, steps: int = 12) -> float:
        """Smooth ramp performed on a background worker, so the caller (the
        engine loop thread) NEVER blocks on the ramp's sleeps. Returns the end
        position immediately; the believed-state update can happen at once while
        the console fader glides there over `ms`."""
        end_db = _clamp(end_db, C.FADER_MIN_DB, C.FADER_MAX_DB)
        self._ensure_ramp_worker()
        self._ramp_q.put((ch, start_db, end_db, ms, steps))
        return end_db

    def _ensure_ramp_worker(self) -> None:
        with self._lock:                      # guard the lazy start against a race
            if self._ramp_q is None:
                self._ramp_q = queue.Queue()
                threading.Thread(target=self._ramp_loop, daemon=True).start()

    def _ramp_loop(self) -> None:
        while True:
            job = self._ramp_q.get()
            if job is None:                   # sentinel from close()
                return
            ch, start_db, end_db, ms, steps = job
            for i in range(1, steps + 1):
                self.set_fader_db(ch, start_db + (end_db - start_db) * (i / steps))
                time.sleep((ms / 1000.0) / steps)

    # -- preamp / clip ------------------------------------------------------
    def nudge_gain_db(self, ch: int, current_db: float, delta_db: float) -> float:
        """Headamp (preamp) trim. /headamp index = (ch-1) on M32."""
        new = _clamp(current_db + delta_db, C.HEADAMP_MIN_DB, C.HEADAMP_MAX_DB)
        with self._lock:
            self._send(f"/headamp/{(ch - 1):03d}/gain", (new + 12.0) / 72.0)
        self._record("gain", ch=ch, db=new)
        return new

    # -- parametric EQ notch (feedback) -------------------------------------
    def set_eq_notch(self, ch: int, band: int, hz: float,
                     gain_db: float = C.FB_NOTCH_GAIN_DB,
                     q: float = C.FB_NOTCH_Q) -> None:
        base = f"/ch/{ch:02d}/eq/{band}"
        with self._lock:
            self._send(f"{base}/type", 2)                  # PEQ
            self._send(f"{base}/f", freq_to_eq_param(hz))
            self._send(f"{base}/g", (gain_db + 15.0) / 30.0)
            self._send(f"{base}/q", q_to_param(q))
            self._send(f"/ch/{ch:02d}/eq/on", 1)
        self._record("notch", ch=ch, band=band, hz=hz, gain_db=gain_db, q=q)

    # -- main-bus EQ (room correction) --------------------------------------
    def set_bus_eq(self, band: int, hz: float, gain_db: float, q: float = 4.0) -> None:
        base = f"/{C.MAIN_BUS}/eq/{band}"
        with self._lock:
            self._send(f"{base}/type", 2)
            self._send(f"{base}/f", freq_to_eq_param(hz))
            self._send(f"{base}/g", (gain_db + 15.0) / 30.0)
            self._send(f"{base}/q", q_to_param(q))
            self._send(f"/{C.MAIN_BUS}/eq/on", 1)
        self._record("bus_eq", band=band, hz=hz, gain_db=gain_db, q=q)

    # -- full parametric EQ band (performer EQ view) ------------------------
    def set_eq_band(self, ch: int, band: int, hz: float, gain_db: float,
                    q: float, on: bool = True) -> None:
        """Set one channel PEQ band to an arbitrary cut/boost (the EQ view), vs
        set_eq_notch which is the feedback-specific deep cut. Same 4 hardware
        bands — they share."""
        base = f"/ch/{ch:02d}/eq/{band}"
        with self._lock:
            self._send(f"{base}/type", 2)                   # PEQ
            self._send(f"{base}/f", freq_to_eq_param(hz))
            self._send(f"{base}/g", (gain_db + 15.0) / 30.0)  # VERIFY: g maps -15..+15 dB
            self._send(f"{base}/q", q_to_param(q))
            self._send(f"/ch/{ch:02d}/eq/on", 1 if on else 0)
        self._record("eq_band", ch=ch, band=band, hz=hz, gain_db=gain_db, q=q, on=on)

    # -- FX / aux sends (performer FX view) ---------------------------------
    def set_send(self, ch: int, bus: int, level_db: float) -> None:
        """Channel send level to a mix/FX bus. VERIFY: X32 channel->bus send addr
        `/ch/NN/mix/BB/level` and its 0..1 mapping against your firmware."""
        with self._lock:
            self._send(f"/ch/{ch:02d}/mix/{bus:02d}/level", db_to_fader(level_db))
        self._record("send", ch=ch, bus=bus, level_db=level_db)

    def set_fx_return(self, fx: int, level_db: float) -> None:
        """FX return (wet) fader. VERIFY: `/fxrtn/NN/mix/fader` address."""
        with self._lock:
            self._send(f"/fxrtn/{fx:02d}/mix/fader", db_to_fader(level_db))
        self._record("fx_return", fx=fx, level_db=level_db)

    # -- scene / mute -------------------------------------------------------
    def recall_scene(self, index: int) -> None:
        with self._lock:
            self._send("/-action/goscene", index)
        self._record("scene", index=index)

    def set_main_mute(self, muted: bool) -> None:
        with self._lock:
            self._send(f"/{C.MAIN_BUS}/mix/on", 0 if muted else 1)
        self._record("main_mute", muted=muted)

    def clear_channel_eq(self, ch: int) -> None:
        """Switch a channel's EQ off (drops any parked feedback notches)."""
        with self._lock:
            self._send(f"/ch/{ch:02d}/eq/on", 0)
        self._record("eq_clear", ch=ch)

    # -- manual surface (human-driven; full physical range, not guardrailed) --
    def set_fader_raw(self, ch: int, db: float) -> float:
        """A human fader move — clamped only to the console's physical range."""
        db = _clamp(db, -90.0, 10.0)
        with self._lock:
            self._send(f"/ch/{ch:02d}/mix/fader", db_to_fader(db))
        self._record("fader", ch=ch, db=db)
        return db

    def set_channel_mute(self, ch: int, muted: bool) -> None:
        with self._lock:
            self._send(f"/ch/{ch:02d}/mix/on", 0 if muted else 1)
        self._record("ch_mute", ch=ch, muted=muted)

    def set_main_fader(self, db: float) -> float:
        db = _clamp(db, -90.0, 10.0)
        with self._lock:
            self._send(f"/{C.MAIN_BUS}/mix/fader", db_to_fader(db))
        self._record("master", db=db)
        return db

    def subscribe_meters(self) -> None:
        """Subscribe to metering AND ask the console to push every parameter
        change to us (/xremote) — that's how a human fader move on the console
        gets reconciled. Both subscriptions time out ~10 s, so the engine renews
        this periodically."""
        with self._lock:
            self._send("/batchsubscribe", "/meters", "/meters/1", 0, 0, 10)
            self._send("/xremote")


# ---------------------------------------------------------------------------
# Real console (UDP)
# ---------------------------------------------------------------------------
class OscConsole(ConsoleBase):
    def __init__(self, ip: str = C.CONSOLE_IP, port: int = C.CONSOLE_PORT) -> None:
        super().__init__()
        self.ip = ip
        try:
            from pythonosc.udp_client import SimpleUDPClient
        except ImportError as e:  # pragma: no cover - hardware path
            raise RuntimeError(
                "python-osc is required for real-console mode: pip install python-osc"
            ) from e
        self.client = SimpleUDPClient(ip, port)

    def _send(self, addr: str, *args) -> None:  # pragma: no cover - needs hw
        # python-osc takes a single value or a list of values.
        self.client.send_message(addr, list(args) if len(args) != 1 else args[0])

    def close(self) -> None:  # pragma: no cover - needs hw
        super().close()                       # stop the ramp worker
        sock = getattr(self.client, "_sock", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Simulated console (records a semantic mirror; no I/O)
# ---------------------------------------------------------------------------
class SimConsole(ConsoleBase):
    """Records every move so the simulator/tests can read console state back."""

    def __init__(self) -> None:
        super().__init__()
        self.wire_log: list[tuple] = []                 # raw (addr, *args)
        self.fader_db: dict[int, float] = {}
        self.gain_db: dict[int, float] = {}
        self.notches: dict[int, list[dict]] = {}        # ch -> [{band,hz,...}]
        self.bus_eq: dict[int, dict] = {}               # band -> {hz,gain_db,q}
        self.main_muted = False
        self.ch_muted: dict[int, bool] = {}
        self.master_db = 0.0
        self.last_scene: int | None = None
        self.eq_bands: dict[int, dict[int, dict]] = {}   # ch -> band -> {hz,gain_db,q,on}
        self.sends: dict[tuple, float] = {}              # (ch,bus) -> level_db
        self.fx_returns: dict[int, float] = {}           # fx -> level_db

    # record raw wire traffic (fidelity) ------------------------------------
    def _send(self, addr: str, *args) -> None:
        self.wire_log.append((addr, *args))

    # record semantic state (introspection) ---------------------------------
    def _record(self, kind: str, **s) -> None:
        with self._lock:                  # keep the semantic mirror self-consistent
            self._apply_record(kind, s)

    def _apply_record(self, kind: str, s: dict) -> None:
        if kind == "fader":
            self.fader_db[s["ch"]] = s["db"]
        elif kind == "gain":
            self.gain_db[s["ch"]] = s["db"]
        elif kind == "notch":
            self.notches.setdefault(s["ch"], [])
            # one band slot is reused; replace any entry on the same band
            self.notches[s["ch"]] = [
                n for n in self.notches[s["ch"]] if n["band"] != s["band"]
            ] + [dict(band=s["band"], hz=s["hz"], gain_db=s["gain_db"], q=s["q"])]
        elif kind == "bus_eq":
            self.bus_eq[s["band"]] = dict(hz=s["hz"], gain_db=s["gain_db"], q=s["q"])
        elif kind == "main_mute":
            self.main_muted = s["muted"]
        elif kind == "scene":
            self.last_scene = s["index"]
        elif kind == "eq_clear":
            self.notches[s["ch"]] = []
        elif kind == "ch_mute":
            self.ch_muted[s["ch"]] = s["muted"]
        elif kind == "master":
            self.master_db = s["db"]
        elif kind == "eq_band":
            self.eq_bands.setdefault(s["ch"], {})[s["band"]] = dict(
                hz=s["hz"], gain_db=s["gain_db"], q=s["q"], on=s["on"])
        elif kind == "send":
            self.sends[(s["ch"], s["bus"])] = s["level_db"]
        elif kind == "fx_return":
            self.fx_returns[s["fx"]] = s["level_db"]

    # ride ramps would block the sim loop with sleeps -> apply instantly
    def ramp_fader_db(self, ch: int, start_db: float, end_db: float,
                      ms: int = C.RAMP_MS, steps: int = 12) -> float:
        return self.set_fader_db(ch, end_db)

    # -- sim introspection helpers ------------------------------------------
    def notch_freqs(self, ch: int) -> list[float]:
        return [n["hz"] for n in self.notches.get(ch, [])]

    def predip_freqs(self) -> list[float]:
        return [b["hz"] for b in self.bus_eq.values() if b["gain_db"] < 0]

    def is_tamed(self, ch: int, hz: float, tol: float = 0.05) -> bool:
        """Is there a cut parked near hz on this channel or the main bus?"""
        if hz <= 0:
            return False
        candidates = self.notch_freqs(ch) + self.predip_freqs()
        return any(abs(hz - f) / hz < tol for f in candidates)
