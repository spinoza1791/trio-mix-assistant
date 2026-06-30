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
    f.fb_freq = detect_ring(mag, sr)
    return f


def detect_ring(mag: np.ndarray, sr: int = C.SAMPLE_RATE) -> float | None:
    """Flag a narrowband peak sitting well above its neighbours (a ring)."""
    if mag.size < 16:
        return None
    db = 20 * np.log10(mag + 1e-9)
    k = int(np.argmax(db))
    if k == 0:
        return None                       # DC is never feedback (and guards /0)
    lo = max(0, k - 6)
    hi = min(db.size, k + 7)
    neighbourhood = np.concatenate([db[lo:k], db[k + 1:hi]])
    if neighbourhood.size == 0:
        return None
    if db[k] - float(np.median(neighbourhood)) > C.FB_RING_DB:
        return k * (sr / 2) / (mag.size - 1)
    return None


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
