"""Pre-show pink-noise room calibration."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config as C
from . import dsp
from .osc import ConsoleBase


@dataclass
class CalibrationResult:
    baseline: dict[float, float] = field(default_factory=dict)   # fc -> dB
    corrections: list[tuple[float, float]] = field(default_factory=list)
    watchlist: list[tuple[float, float]] = field(default_factory=list)

    @property
    def watch_freqs(self) -> list[float]:
        return [f for f, _ in self.watchlist]


class Calibrator:
    """Runs the pre-show pink-noise pass and applies gentle room correction."""

    def __init__(self, console: ConsoleBase) -> None:
        self.con = console

    def analyze(self, captured: np.ndarray) -> CalibrationResult:
        """Pure analysis of a captured pink-noise recording. No console I/O, no
        shared state — heavy FFTs here are safe to run OFF any lock."""
        baseline = dsp.octave_band_levels(captured)
        corrections, watchlist = dsp.find_resonant_peaks(captured)
        return CalibrationResult(baseline=baseline, corrections=corrections,
                                 watchlist=watchlist)

    def apply(self, res: CalibrationResult, log=print) -> set[int]:
        """Park room-correction cuts + feedback pre-dips on the main bus. Fast
        (a handful of OSC sends) — call this under the engine lock. Returns the
        set of main-bus bands it claimed, so live feedback notching can avoid
        clobbering them."""
        used: set[int] = set()
        corrected = set()
        # Start from a clean main-bus EQ: flatten all 6 bands first. This means a
        # re-calibration leaves no ghost cuts and any live feedback notch parked
        # on the bus is cleared (the assistant resets its feedback-band tracking
        # to match). `used` returns only the bands carrying a real cut.
        for band in range(1, 7):
            self.con.set_bus_eq(band, 1000.0, 0.0, q=2.0)
        # gentle room-peak correction (bands 1..4)
        for band, (fc, cut) in enumerate(res.corrections[:4], start=1):
            self.con.set_bus_eq(band, fc, cut, q=3.0)
            corrected.add(fc)
            used.add(band)
            log(f"bus EQ {band}: {fc:.0f} Hz {cut:+.1f} dB")
        # pre-dip the worst feedback-prone freqs (bands 5..6) — but never a band
        # a room correction already cut (no double-dipping the same freq).
        n_predip = 0
        for fc, excess in res.watchlist:
            if n_predip >= C.CAL_N_PREDIP:
                break
            if fc in corrected:
                continue
            band = 5 + n_predip
            self.con.set_bus_eq(band, fc, C.CAL_PREDIP_DB, q=6.0)
            used.add(band)
            log(f"pre-dip {band}: {fc:.0f} Hz {C.CAL_PREDIP_DB:+.1f} dB "
                f"(excess {excess:.1f} dB)")
            n_predip += 1
        return used

    def run(self, capture_meas_mic, emit_pink_noise=None, apply: bool = True,
            log=print) -> CalibrationResult:
        """Emit (optional) + capture + analyze + optionally apply. Used by tests;
        the engine instead splits analyze (off-lock) from apply (on-lock)."""
        if emit_pink_noise:
            emit_pink_noise(dsp.generate_pink_noise(C.CAL_DURATION_S))
        res = self.analyze(capture_meas_mic())
        if apply:
            self.apply(res, log=log)
        return res
