"""DSP feature extraction + pink-noise calibration math.

Pure, side-effect-free numpy. Everything here is deterministic and unit-tested.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import config as C


# ---------------------------------------------------------------------------
# Per-block channel features
# ---------------------------------------------------------------------------
@dataclass
class ChannelFeatures:
    rms_dbfs: float = -90.0
    peak_dbfs: float = -90.0
    fb_freq: float | None = None          # flagged ringing frequency, if any
    contrast_db: float = 0.0              # spectral peakiness (max-median); SNR proxy
    harmonicity: float = 0.0              # 0=pure tone (feedback-like) .. 1=harmonic (musical)
    spectrum: np.ndarray = field(default_factory=lambda: np.zeros(0))


def analyse_block(samples: np.ndarray, sr: int = C.SAMPLE_RATE) -> ChannelFeatures:
    """One channel, one block -> features."""
    if samples.size == 0:
        return ChannelFeatures()
    rms = math.sqrt(float(np.mean(samples ** 2))) + 1e-12
    peak = float(np.max(np.abs(samples))) + 1e-12
    f = ChannelFeatures(
        rms_dbfs=20 * math.log10(rms),
        peak_dbfs=20 * math.log10(peak),
    )
    win = samples * np.hanning(samples.size)
    mag = np.abs(np.fft.rfft(win))
    f.spectrum = mag
    # spectral contrast = how far the hottest bin sits above the typical bin.
    # High = clear tonal content (a ring is detectable); low = broadband/noisy
    # (a loud crowd) -> the room mic's feedback SNR is poor.
    if mag.size:
        db = 20 * np.log10(mag + 1e-9)
        f.contrast_db = float(np.max(db) - np.median(db))
        k = _find_ring_bin(db)            # share one FFT for freq + harmonicity
        if k is not None:
            f.fb_freq = _bin_to_hz(_parabolic_peak(db, k), mag.size, sr)
            f.harmonicity = _harmonicity(db, k)
    return f


def _bin_to_hz(k: float, nbins: int, sr: int) -> float:
    """Convert a (possibly fractional) rfft bin index to Hz."""
    return k * (sr / 2) / (nbins - 1)


def _parabolic_peak(db: np.ndarray, k: int) -> float:
    """Sub-bin peak location via parabolic interpolation on the log-magnitude
    spectrum (Smith/Serra). A raw FFT bin index quantises the frequency to
    sr/N steps (~47 Hz at N=1024/48k) — fine at 2 kHz but ±12% at 200 Hz, so a
    notch lands off the ring. Fitting a parabola to the peak bin and its two
    neighbours recovers the true frequency to a fraction of a bin, for free."""
    if k <= 0 or k >= db.size - 1:
        return float(k)
    a, b, c = float(db[k - 1]), float(db[k]), float(db[k + 1])
    denom = a - 2.0 * b + c
    if denom == 0.0:
        return float(k)                   # flat top -> no better estimate than k
    delta = 0.5 * (a - c) / denom
    if not -0.5 <= delta <= 0.5:          # ill-conditioned -> fall back to the bin
        return float(k)
    return k + delta


def _find_ring_bin(db: np.ndarray) -> int | None:
    """Index of a narrowband peak sitting well above its neighbours (a ring), or
    None. `db` is 20*log10(magnitude). Resolution-agnostic: works on a 1024-pt
    block spectrum or a longer high-res window alike."""
    if db.size < 16:
        return None
    k = int(np.argmax(db))
    if k == 0:
        return None                       # DC is never feedback (and guards /0)
    lo = max(0, k - 6)
    hi = min(db.size, k + 7)
    neighbourhood = np.concatenate([db[lo:k], db[k + 1:hi]])
    if neighbourhood.size == 0:
        return None
    if db[k] - float(np.median(neighbourhood)) > C.FB_RING_DB:
        return k
    return None


def _harmonicity(db: np.ndarray, k: int) -> float:
    """0..1 estimate of how much of a harmonic series sits above the peak.

    Acoustic feedback is a near-pure tone (no harmonic partials); a sustained
    musical note (organ, bowed string, held vocal) is *also* a narrowband peak
    but carries strong 2nd/3rd harmonics. Requiring BOTH partials (we take the
    weaker) makes this a robust "is this music?" signal that the assistant uses
    to demand more sustain before notching — so it can't be fooled into cutting
    a held note. ~0 for a pure ring, ->1 for a rich musical tone."""
    floor = float(np.median(db))
    fund = float(db[k]) - floor
    if fund <= 0.0:
        return 0.0
    partials = []
    for h in (2, 3):
        kb = k * h
        if kb < db.size:
            partials.append(max(0.0, float(db[kb]) - floor))
    if len(partials) < 2:                 # fundamental too high to see 2 harmonics
        return 0.0
    return float(min(1.0, min(partials) / fund))


def detect_ring(mag: np.ndarray, sr: int = C.SAMPLE_RATE) -> float | None:
    """Flag a narrowband ring and return its (sub-bin-interpolated) frequency."""
    if mag.size < 16:
        return None
    db = 20 * np.log10(mag + 1e-9)
    k = _find_ring_bin(db)
    if k is None:
        return None
    return _bin_to_hz(_parabolic_peak(db, k), mag.size, sr)


def ring_harmonicity(mag: np.ndarray) -> float:
    """Public wrapper: harmonicity (0..1) of the dominant ring in a spectrum."""
    if mag.size < 16:
        return 0.0
    db = 20 * np.log10(mag + 1e-9)
    k = _find_ring_bin(db)
    return 0.0 if k is None else _harmonicity(db, k)


class RollingRingDetector:
    """Higher-resolution feedback detection over a rolling analysis window.

    The engine's per-block FFT (BLOCK=1024 -> ~47 Hz/bin) is too coarse to place
    a notch accurately on low / low-mid feedback. This keeps a rolling buffer of
    the most recent `fft_size` samples and runs the ring detector on it (e.g.
    4096 -> ~11.7 Hz/bin) WITHOUT adding detection latency: it still decides
    every block on the newest data, and sub-bin interpolation refines further.
    Stateful — one instance per analysed channel (the engine uses it for the
    measurement mic, where feedback is acoustic and the resolution gain matters
    most). Falls back to whatever history it has until the buffer fills."""

    def __init__(self, fft_size: int | None = None, sr: int = C.SAMPLE_RATE) -> None:
        self.n = int(fft_size or C.FB_DETECT_FFT)
        self.sr = sr
        self._buf = np.zeros(0, dtype=np.float32)
        self._win = np.hanning(self.n)

    def update(self, samples: np.ndarray) -> tuple[float | None, float]:
        """Push a new block; return (fb_freq | None, harmonicity) at high res."""
        if samples.size:
            self._buf = np.concatenate([self._buf, samples])[-self.n:]
        buf = self._buf
        if buf.size < 16:
            return None, 0.0
        win = buf * (self._win if buf.size == self.n else np.hanning(buf.size))
        mag = np.abs(np.fft.rfft(win))
        db = 20 * np.log10(mag + 1e-9)
        k = _find_ring_bin(db)
        if k is None:
            return None, 0.0
        return _bin_to_hz(_parabolic_peak(db, k), mag.size, self.sr), _harmonicity(db, k)


# ---------------------------------------------------------------------------
# Phase / polarity relationship between two channels (comb-filter / phase cancel)
# ---------------------------------------------------------------------------
@dataclass
class PhaseRelation:
    """How two channels that (may) carry the same source relate in time/polarity.

    zero_corr : normalised zero-lag correlation, in [-1, 1]. Its SIGN is polarity:
                ~+1 in phase, ~-1 inverted (summing them cancels), ~0 unrelated.
    best_corr : normalised correlation at the best lag (how correlated they are at
                all, once any time offset is removed).
    lag_ms    : the arrival-time offset of the best alignment; a non-zero lag with
                high correlation is the signature of comb filtering.
    """
    zero_corr: float = 0.0
    best_corr: float = 0.0
    lag_samples: int = 0
    lag_ms: float = 0.0


def phase_relation(a: np.ndarray, b: np.ndarray, sr: int = C.SAMPLE_RATE,
                   max_lag_ms: float | None = None) -> PhaseRelation:
    """Cross-correlate two channel blocks to measure polarity + time offset.

    Pure + deterministic. Returns zeros for silence or too-short input. The
    zero-lag correlation's sign detects a polarity flip; the best-lag position
    detects a comb-filtering time offset."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    n = int(min(a.size, b.size))
    if n < 16:
        return PhaseRelation()
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    na = float(np.sqrt(np.dot(a, a)))
    nb = float(np.sqrt(np.dot(b, b)))
    if na < 1e-9 or nb < 1e-9:                       # one side is silent
        return PhaseRelation()
    denom = na * nb
    zero = float(np.dot(a, b)) / denom               # sign = polarity (convention-free)
    max_lag = int((C.PHASE_MAX_LAG_MS if max_lag_ms is None else max_lag_ms)
                  / 1000.0 * sr)
    max_lag = max(1, min(max_lag, n - 1))
    full = np.correlate(a, b, mode="full") / denom   # length 2n-1, centre = zero lag
    centre = n - 1
    window = full[centre - max_lag: centre + max_lag + 1]
    k = int(np.argmax(np.abs(window)))
    lag = k - max_lag                                # samples from zero lag
    return PhaseRelation(zero_corr=zero, best_corr=float(window[k]),
                         lag_samples=lag, lag_ms=lag / sr * 1000.0)


# ---------------------------------------------------------------------------
# Pink-noise + octave-band analysis (calibration front-end)
# ---------------------------------------------------------------------------
def generate_pink_noise(seconds: float, sr: int = C.SAMPLE_RATE,
                        level_dbfs: float = C.CAL_NOISE_DBFS,
                        rng: np.random.Generator | None = None) -> np.ndarray:
    """Pink noise via 1/sqrt(f) shaping of white noise."""
    n = int(seconds * sr)
    rng = rng or np.random.default_rng()
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    freqs[0] = freqs[1]
    spectrum /= np.sqrt(freqs)
    pink = np.fft.irfft(spectrum, n)
    pink /= (np.max(np.abs(pink)) + 1e-9)
    pink *= 10 ** (level_dbfs / 20.0)
    return pink.astype(np.float32)


def octave_band_levels(samples: np.ndarray, sr: int = C.SAMPLE_RATE) -> dict[float, float]:
    """Return dB level in each calibration octave band."""
    win = samples * np.hanning(samples.size)
    mag = np.abs(np.fft.rfft(win))
    freqs = np.fft.rfftfreq(samples.size, 1 / sr)
    out: dict[float, float] = {}
    for fc in C.CAL_OCTAVE_BANDS:
        lo, hi = fc / math.sqrt(2), fc * math.sqrt(2)
        sel = (freqs >= lo) & (freqs < hi)
        if np.any(sel):
            out[fc] = 20 * math.log10(math.sqrt(float(np.mean(mag[sel] ** 2))) + 1e-9)
    return out


def find_resonant_peaks(captured: np.ndarray, sr: int = C.SAMPLE_RATE):
    """Compare captured pink-noise spectrum to its own smoothed 'house curve'.

    Bands sitting CAL_PEAK_THRESH above the smoothed average are room/PA peaks;
    the sharpest of these are the feedback-prone freqs.

    Returns (corrections, watchlist):
        corrections = [(fc, cut_db), ...]   gentle main-bus cuts
        watchlist   = [(fc, excess_db), ...] ranked hottest-first
    """
    bands = octave_band_levels(captured, sr)
    fcs = list(bands.keys())
    vals = np.array([bands[f] for f in fcs])
    avg = np.convolve(vals, np.ones(3) / 3, mode="same")  # smoothed house curve
    corrections, watch = [], []
    for i, (fc, v, a) in enumerate(zip(fcs, vals, avg)):
        if i == 0 or i == len(fcs) - 1:      # edge bands: smoothing unreliable
            continue
        excess = v - a
        if excess > C.CAL_PEAK_THRESH:
            cut = max(C.CAL_MAX_CUT_DB, -excess)
            corrections.append((fc, round(cut, 1)))
            watch.append((fc, round(float(excess), 1)))
    watch.sort(key=lambda x: x[1], reverse=True)
    return corrections, watch
