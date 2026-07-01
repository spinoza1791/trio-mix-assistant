"""Audio capture sources — the *listening half* of the system.

The engine pulls one analysis frame ({console_ch: float array of length BLOCK})
per tick from a CaptureSource. Implementations:

  * SimCapture          — the built-in closed-loop simulator (no hardware).
  * SoundDeviceCapture  — real multichannel capture via PortAudio/sounddevice
                          (ASIO/WASAPI): the console's USB/card stream plus a FOH
                          measurement mic. The audio callback only copies into a
                          bounded queue (it never blocks or allocates much);
                          block() pops the most recent frame and drops any backlog
                          so the engine stays real-time.

`block()` must be cheap and non-blocking (the engine calls it under its lock).
`capture_meas()` may block for seconds (it's called off-lock, during calibration).
"""
from __future__ import annotations

import queue
import threading
import time

import numpy as np

from . import config as C
from . import dsp


class CaptureError(RuntimeError):
    """A capture device could not be opened — message is operator-facing."""


class CaptureSource:
    """Interface the engine pulls analysis frames from."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def block(self) -> dict[int, np.ndarray]:
        """One analysis frame. MUST NOT block (called under the engine lock)."""
        raise NotImplementedError

    def capture_meas(self, seconds: float) -> np.ndarray | None:
        """Record `seconds` of the measurement-mic channel for calibration. May
        block (called off-lock). Returns None if no meas-mic input is available."""
        return None

    def emit_pink_noise(self, samples: np.ndarray) -> bool:
        """Optionally play the calibration pink noise out (the audio output).
        Returns True if playback was issued (or N/A for sim), False on failure."""
        return True

    # -- health hooks (uniform across all sources; real device overrides) ---
    def dead(self) -> bool:
        """True if the stream has stopped delivering audio (device gone/wedged)."""
        return False

    def silent(self) -> bool:
        """True if the stream IS delivering frames but they're digital-silence —
        the tell-tale of a denied mic permission (macOS), a muted/disconnected
        input, or zero input gain. The device looks fine but we hear nothing."""
        return False

    def restart(self) -> bool:
        """Try to re-open a dead device. Returns True on success."""
        return False

    def status(self) -> dict:
        """Health for telemetry (kind, overruns, underruns, ...)."""
        return {"kind": "none"}


# ---------------------------------------------------------------------------
# Simulation source (closed loop: reads console state back to tame feedback)
# ---------------------------------------------------------------------------
def sim_room_capture(rng=None) -> np.ndarray:
    """A synthetic measurement-mic recording for the pink-noise calibration:
    pink noise + a boomy 250 Hz room mode + a hot 2.5 kHz resonance."""
    noise = dsp.generate_pink_noise(C.CAL_DURATION_S, rng=rng)
    t = np.arange(noise.size) / C.SAMPLE_RATE
    noise = noise + 0.30 * np.sin(2 * np.pi * 250.0 * t)
    noise = noise + 0.22 * np.sin(2 * np.pi * 2500.0 * t)
    return noise


class SimCapture(CaptureSource):
    NOISE = 0.008
    RING_FREQ = 2500.0          # a calibration watch-list frequency
    BOOM_FREQ = 250.0           # steady room mode — must NOT be flagged

    def __init__(self, console, seed: int = 7) -> None:
        self.con = console
        self.rng = np.random.default_rng(seed)
        self.n = 0
        self.block_i = 0
        self.ring_amp = 0.0
        self.next_ring = 40
        self.clip_until = -1
        self.next_clip = 90

    def _gaintrim_lin(self, ch: int) -> float:
        trim = self.con.gain_db.get(ch, 20.0) - 20.0   # dB below nominal
        return 10 ** (trim / 20.0)

    def block(self) -> dict[int, np.ndarray]:
        t = (self.n + np.arange(C.BLOCK)) / C.SAMPLE_RATE
        self.n += C.BLOCK
        self.block_i += 1
        i = self.block_i
        frame: dict[int, np.ndarray] = {}

        for ch, role in C.CHANNELS.items():
            x = self.NOISE * self.rng.standard_normal(C.BLOCK)
            lin = self._gaintrim_lin(ch)
            if role == "lead_vox":
                env = 10 ** ((3.0 * np.sin(2 * np.pi * t / 20.0)) / 20.0)   # slow ±3 dB
                vib = 220.0 * (1 + 0.015 * np.sin(2 * np.pi * 5.0 * t))      # vibrato
                x = x + 0.50 * env * np.sin(2 * np.pi * vib * t)
            elif role == "harm_vox_l":
                x = x + 0.06 * np.sin(2 * np.pi * 330.0 * t)
            elif role == "harm_vox_r":
                x = x + 0.06 * np.sin(2 * np.pi * 440.0 * t)
            elif role == "acoustic_gtr":
                x = x + 0.09 * np.sin(2 * np.pi * 196.0 * t) + 0.03 * self.rng.standard_normal(C.BLOCK)
            elif role == "bass_di":
                x = x + 0.12 * np.sin(2 * np.pi * 82.0 * t)
            elif role == "cajon":
                x = x + 0.05 * self.rng.standard_normal(C.BLOCK)
            elif role == "keys_aux":
                x = x + 0.07 * np.sin(2 * np.pi * 262.0 * t)
            if role == "cajon" and i <= self.clip_until:
                x = x + 0.95 * np.sin(2 * np.pi * 700.0 * t)
            frame[ch] = (x * lin).astype(np.float32)

        # the room, heard at the measurement mic
        room = (self.NOISE * self.rng.standard_normal(C.BLOCK)
                + 0.055 * np.sin(2 * np.pi * self.BOOM_FREQ * t))
        if i >= self.next_ring and self.ring_amp == 0.0:
            self.ring_amp = 0.05
        if self.ring_amp > 0.0:
            if self.con.is_tamed(C.MEAS_MIC_CH, self.RING_FREQ):
                self.ring_amp *= 0.55
                if self.ring_amp < 0.01:
                    self.ring_amp = 0.0
                    self.next_ring = i + 120
            else:
                self.ring_amp = min(0.9, self.ring_amp * 1.25)
            room = room + self.ring_amp * np.sin(2 * np.pi * self.RING_FREQ * t)
        frame[C.MEAS_MIC_CH] = room.astype(np.float32)

        if i >= self.next_clip and i > self.clip_until:
            self.clip_until = i + 5
            self.next_clip = i + 160
        return frame

    def capture_meas(self, seconds: float) -> np.ndarray:
        return sim_room_capture(self.rng)

    def status(self) -> dict:
        return {"kind": "simulation"}


# ---------------------------------------------------------------------------
# Real multichannel capture (PortAudio / sounddevice)
# ---------------------------------------------------------------------------
class SoundDeviceCapture(CaptureSource):
    """Live multichannel capture. The console's USB/card stream + the FOH meas
    mic arrive on one (or several) input device channels. `channel_map` maps each
    console channel to a 0-based device channel index (default: console N -> dev
    N-1). Pass `stream_factory` to inject a fake stream in tests."""

    def __init__(self, device=None, channel_map: dict[int, int] | None = None,
                 samplerate: int = C.SAMPLE_RATE, block: int = C.BLOCK,
                 meas_ch: int | None = None, output_device=None,
                 stream_factory=None, output_factory=None, gain_db: float = 0.0) -> None:
        self.device = device
        self.output_device = output_device
        # digital input trim — for quiet interfaces / dynamic mics (e.g. Samson Q9U)
        self.gain_db = float(gain_db)
        self._gain = 10.0 ** (self.gain_db / 20.0)
        self._auto_db = None            # level auto_gain measured (None = no audio at all)
        self.samplerate = samplerate
        self.blocksize = block          # NB: 'block' is also the method name
        # resolve at call time, NOT as a default arg — C.MEAS_MIC_CH changes when a
        # template / auto map is applied, and a frozen import-time default (8) would
        # point at a channel the map no longer has, breaking capture_meas.
        self.meas_ch = C.MEAS_MIC_CH if meas_ch is None else meas_ch
        chans = sorted(C.CHANNELS)
        self.channel_map = channel_map or {ch: ch - 1 for ch in chans}
        self.ndev = max(self.channel_map.values()) + 1     # device input channels to open
        self._q: queue.Queue = queue.Queue(maxsize=32)
        self._stream = None
        self._stream_lock = threading.Lock()         # serialize open/close vs restart()
        self._stream_factory = stream_factory
        self._output_factory = output_factory
        self._last: np.ndarray | None = None
        self._overruns = 0
        self._underruns = 0
        self._consec_underruns = 0
        self._silent_since: float | None = None     # when input first went dead-silent
        self._peak = 0.0                             # most recent frame peak (telemetry)
        self._ovr_mark_t: float | None = None        # xrun-rate window
        self._ovr_mark_val = 0
        self._overload = False
        self._dev_name: str | None = None            # resolved device name (catches index shift)

    # -- audio thread (keep it trivial: copy into the queue, drop on overflow) --
    def _on_audio(self, indata, frames=None, time_info=None, status=None) -> None:
        if status:
            self._overruns += 1
        item = np.asarray(indata, dtype="float32").copy()
        if self._gain != 1.0:
            item *= self._gain                             # digital input trim
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self._overruns += 1                            # consumer fell behind:
            try:                                           # drop the OLDEST, keep newest
                self._q.get_nowait()
                self._q.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        # _stream_lock serializes open/close so a recovery restart() on the loop
        # thread can't race a stop() from engine shutdown.
        with self._stream_lock:
            if self._stream is not None:
                return
            factory = self._stream_factory or self._make_input_stream
            stream = factory(self._on_audio)        # may raise CaptureError (left None)
            stream.start()
            self._stream = stream

    def _make_input_stream(self, callback):                # pragma: no cover - needs hw
        try:
            import sounddevice as sd
        except ImportError as e:
            raise CaptureError(
                "the 'sounddevice' package isn't installed — run setup "
                "(pip install sounddevice)") from e
        # Pre-flight: a clear "device has N inputs, need M" / "device not found"
        # beats PortAudio's opaque error codes.
        try:
            info = sd.query_devices(self.device, "input")
            self._dev_name = info.get("name")
            maxin = int(info.get("max_input_channels", 0))
            if maxin < self.ndev:
                raise CaptureError(
                    f"audio device '{info.get('name', self.device)}' has {maxin} input "
                    f"channel(s) but this setup needs {self.ndev} "
                    f"(console returns + meas mic). Pick a fuller device or aggregate.")
        except CaptureError:
            raise
        except Exception as e:                  # device index/name not found, etc.
            raise CaptureError(
                f"audio device {self.device!r} could not be opened "
                f"({type(e).__name__}: {e}). Run --list-devices and check --audio-device."
            ) from e
        try:
            return sd.InputStream(device=self.device, channels=self.ndev,
                                  samplerate=self.samplerate, blocksize=self.blocksize,
                                  dtype="float32", callback=callback)
        except Exception as e:                  # busy/exclusive, samplerate unsupported, ...
            raise CaptureError(
                f"could not open audio device {self.device!r} at {self.samplerate} Hz "
                f"({type(e).__name__}: {e}). It may be in use by another app, or not "
                f"support {self.samplerate} Hz.") from e

    def stop(self) -> None:
        with self._stream_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    # -- frames -------------------------------------------------------------
    def _map(self, frame: np.ndarray) -> dict[int, np.ndarray]:
        n = frame.shape[0]
        ncols = frame.shape[1] if frame.ndim == 2 else 1
        out: dict[int, np.ndarray] = {}
        for ch, dev in self.channel_map.items():
            if frame.ndim == 2 and dev < ncols:
                out[ch] = np.ascontiguousarray(frame[:, dev], dtype="float32")
            else:
                out[ch] = np.zeros(n, dtype="float32")
        return out

    SILENCE_AFTER = 4          # consecutive empty ticks -> stream is dead, emit silence
    SILENCE_FLOOR = 1e-4       # whole-frame peak below this == digital silence (~-80 dBFS);
                               # any one live channel clears it, so no quiet-passage false +ve
    SILENCE_HOLD_S = 5.0       # sustained that long while the stream runs -> flag "no audio"

    def block(self) -> dict[int, np.ndarray]:
        frame = None
        while True:                                        # take the most recent, drop backlog
            try:
                frame = self._q.get_nowait()
            except queue.Empty:
                break
        if frame is not None:
            self._consec_underruns = 0
            self._last = frame
            self._track_silence(frame)
        else:
            self._underruns += 1
            self._consec_underruns += 1
            # Briefly reuse the last frame (smooths jitter); but if the stream has
            # gone silent for a while (device unplugged / driver wedged) emit real
            # silence so the assistant can't keep "hearing" a frozen frame.
            if self._consec_underruns > self.SILENCE_AFTER or self._last is None:
                frame = np.zeros((self.blocksize, self.ndev), "float32")
            else:
                frame = self._last
        return self._map(frame)

    def _track_silence(self, frame: np.ndarray) -> None:
        self._peak = float(np.max(np.abs(frame))) if frame.size else 0.0
        if self._peak >= self.SILENCE_FLOOR:
            self._silent_since = None
        elif self._silent_since is None:
            self._silent_since = time.monotonic()

    def dead(self) -> bool:
        """True if the input stream has stopped delivering audio (stale)."""
        return self._consec_underruns > self.SILENCE_AFTER

    def silent(self) -> bool:
        """Stream is delivering frames but they're digital silence for a while —
        the signature of a denied mic permission, a muted input, or zero gain."""
        if self._stream is None or self.dead() or self._silent_since is None:
            return False
        return (time.monotonic() - self._silent_since) > self.SILENCE_HOLD_S

    OVERLOAD_RATE = 10.0       # sustained xruns/sec -> the buffer is too small

    def overload(self) -> bool:
        """True if the interface is dropping samples (xruns) at a sustained rate —
        usually an undersized device buffer. Evaluated on a ~2 s window."""
        now = time.monotonic()
        if self._ovr_mark_t is None:
            self._ovr_mark_t, self._ovr_mark_val = now, self._overruns
            return False
        dt = now - self._ovr_mark_t
        if dt >= 2.0:
            rate = (self._overruns - self._ovr_mark_val) / dt
            self._ovr_mark_t, self._ovr_mark_val = now, self._overruns
            self._overload = rate > self.OVERLOAD_RATE
        return self._overload

    def restart(self) -> bool:
        """Re-open the input stream (e.g. after a USB unplug/replug)."""
        self.stop()
        try:
            self.start()
            self._consec_underruns = 0
            self._silent_since = None
            return True
        except Exception:
            return False

    def capture_meas(self, seconds: float) -> np.ndarray | None:
        dev = self.channel_map.get(self.meas_ch)
        if dev is None:
            return None
        need = int(seconds * self.samplerate)
        chunks, got = [], 0
        deadline = time.monotonic() + seconds + 3.0
        while got < need and time.monotonic() < deadline:
            try:
                frame = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame.ndim == 2 and dev < frame.shape[1]:
                col = np.ascontiguousarray(frame[:, dev], dtype="float32")
                chunks.append(col)
                got += col.size
        if not chunks:
            return None
        return np.concatenate(chunks)[:need].astype(np.float32)

    def auto_gain(self, target_dbfs: float = -32.0, seconds: float = 0.6,
                  max_db: float = 48.0) -> float:
        """Sample a moment of the meas mic and set the digital input gain to bring
        a quiet (e.g. dynamic) mic up to ~target. Call right after start(), before
        the engine loop drains the queue. Returns the gain applied (dB)."""
        self.gain_db, self._gain = 0.0, 1.0               # measure raw level
        time.sleep(0.4)                                   # let the stream settle
        rec = self.capture_meas(seconds)
        if rec is None or not rec.size:
            self._auto_db = None                          # the mic delivered nothing
            return 0.0
        rms = float(np.sqrt(np.mean(rec.astype(np.float64) ** 2))) + 1e-9
        self._auto_db = 20.0 * np.log10(rms)
        boost = max(0.0, min(max_db, target_dbfs - self._auto_db))
        self.gain_db = round(float(boost), 1)
        self._gain = 10.0 ** (self.gain_db / 20.0)
        return self.gain_db

    def emit_pink_noise(self, samples: np.ndarray) -> bool:
        try:                                               # pragma: no cover - needs hw
            if self._output_factory:
                self._output_factory(samples, self.samplerate)
                return True
            import sounddevice as sd
            sd.play(samples, self.samplerate, device=self.output_device)
            return True
        except Exception:
            return False                                   # no/failed output device

    def status(self) -> dict:
        return {"kind": "audio", "overruns": self._overruns, "underruns": self._underruns,
                "dead": self.dead(), "silent": self.silent(), "overload": self.overload(),
                "peak": round(self._peak, 5), "name": self._dev_name,
                "device": str(self.device), "channels": self.ndev}


def _rank_inputs(devs, hostapis, default_name, prefer_sr: int = C.SAMPLE_RATE):
    """Rank input-capable devices best-first. Pure + testable. Returns a list of
    {index, name, channels, samplerate, hostapi} dicts.

    The same physical mic often appears once per host API (MME/DirectSound/WASAPI
    on Windows); this prefers the entry that natively matches the app's rate and a
    low-latency API, and an external mic over a built-in laptop array."""
    cands = [(i, d) for i, d in enumerate(devs) if d.get("max_input_channels", 0) > 0]

    def api_name(d):
        h = d.get("hostapi")
        return hostapis[h]["name"] if isinstance(h, int) and 0 <= h < len(hostapis) else ""

    def score(item):
        _i, d = item
        name = (d.get("name") or "")
        s = 0.0
        if default_name and name == default_name:
            s += 10.0                                       # the OS default input
        if int(round(d.get("default_samplerate", 0) or 0)) == prefer_sr:
            s += 5.0                                        # native at the app's rate
        api = api_name(d)
        if "ASIO" in api:
            s += 4.0                                        # lowest-latency pro path; the
                                                            # native driver for multichannel
                                                            # interfaces (console X-USB cards)
        elif any(k in api for k in ("WASAPI", "Core Audio", "ALSA", "JACK")):
            s += 3.0                                        # low-latency / no resampling
        if any(k in name.lower() for k in ("array", "built-in", "built in", "internal")):
            s -= 15.0                                       # prefer a plugged-in external mic
        s += min(2.0, d.get("max_input_channels", 0) * 0.01)   # tiebreak: more inputs
        return s

    ranked = sorted(cands, key=score, reverse=True)
    return [{"index": i, "name": d.get("name"),
             "channels": int(d.get("max_input_channels", 1)),
             "samplerate": int(round(d.get("default_samplerate", prefer_sr) or prefer_sr)),
             "hostapi": api_name(d)} for i, d in ranked]


def _pick_input(devs, hostapis, default_name, prefer_sr: int = C.SAMPLE_RATE):
    r = _rank_inputs(devs, hostapis, default_name, prefer_sr)
    return r[0] if r else None


def autodetect_inputs(prefer_sr: int = C.SAMPLE_RATE):     # pragma: no cover - needs hw
    """Ranked list (best-first) of input devices on this machine (Windows/macOS).
    No device is opened here — the caller opens each in order for real and keeps
    the first that actually delivers audio, so a contended/dead entry is skipped
    without the double-open that WASAPI doesn't tolerate. Returns a list of dicts."""
    try:
        import sounddevice as sd
        devs = list(sd.query_devices())
        apis = list(sd.query_hostapis())
        din = sd.default.device
        di = din[0] if isinstance(din, (list, tuple)) else din
        name = devs[di].get("name") if isinstance(di, int) and 0 <= di < len(devs) else None
        return _rank_inputs(devs, apis, name, prefer_sr)
    except Exception:
        return []


def parse_channel_map(spec: str) -> dict[int, int]:
    """Parse a '--channel-map' string into a {console_ch: device_column} map.

    Format: comma-separated `console:device` pairs, e.g. '1:0,2:1,8:9' means
    console channel 8 (the meas mic) is on device input column 9. Console
    channels are 1-based; device columns are 0-based. Raises ValueError on
    malformed input so the CLI can fail fast with a clear message."""
    out: dict[int, int] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(f"entry '{pair}' must be console:device (e.g. 8:9)")
        c, d = pair.split(":", 1)
        try:
            ch, dev = int(c), int(d)
        except ValueError:
            raise ValueError(f"entry '{pair}' has non-integer channel/column")
        if ch < 1 or dev < 0:
            raise ValueError(f"entry '{pair}': console channel >= 1, device column >= 0")
        out[ch] = dev
    if not out:
        raise ValueError("channel-map is empty")
    return out


def _format_device_list(devs, apis) -> str:
    """Pure formatter for --list-devices (host API included so an operator can
    pick the right WASAPI/ASIO instance of a device). Split out to be testable
    without hardware; list_audio_devices() feeds it live sounddevice data."""
    lines = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            h = d.get("hostapi")
            api = apis[h]["name"] if isinstance(h, int) and 0 <= h < len(apis) else "?"
            lines.append(f"  [{i}] {d['name']}  ({d['max_input_channels']} in, "
                         f"{int(d.get('default_samplerate', 0))} Hz, {api})")
    return "Input devices:\n" + ("\n".join(lines) or "  (none found)")


def list_audio_devices() -> str:                           # pragma: no cover - needs hw
    """Human-readable list of input audio devices (for --list-devices)."""
    try:
        import sounddevice as sd
        return _format_device_list(list(sd.query_devices()), list(sd.query_hostapis()))
    except Exception as e:
        return f"could not list audio devices: {e}"
