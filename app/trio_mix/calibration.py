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

    def plan(self, res: CalibrationResult) -> list[dict]:
        """The main-bus PEQ moves calibration would make: gentle room-peak cuts on
        bands 1..4, then feedback pre-dips on bands 5..6 (never re-cutting a freq a
        room correction already handled). This is the single source of truth for
        both apply() (which writes them) and coach mode (which advises them), so the
        console EQ and the advised EQ can never diverge."""
        steps: list[dict] = []
        corrected = set()
        for band, (fc, cut) in enumerate(res.corrections[:4], start=1):
            steps.append({"band": band, "hz": fc, "gain": cut, "q": 3.0, "kind": "room"})
            corrected.add(fc)
        n_predip = 0
        for fc, excess in res.watchlist:
            if n_predip >= C.CAL_N_PREDIP:
                break
            if fc in corrected:
                continue
            steps.append({"band": 5 + n_predip, "hz": fc, "gain": C.CAL_PREDIP_DB,
                          "q": 6.0, "kind": "predip", "excess": excess})
            n_predip += 1
        return steps

    def apply(self, res: CalibrationResult, log=print) -> set[int]:
        """Park room-correction cuts + feedback pre-dips on the main bus. Fast
        (a handful of OSC sends) — call this under the engine lock. Returns the
        set of main-bus bands it claimed, so live feedback notching can avoid
        clobbering them."""
        # Start from a clean main-bus EQ: flatten all 6 bands first. This means a
        # re-calibration leaves no ghost cuts and any live feedback notch parked
        # on the bus is cleared (the assistant resets its feedback-band tracking
        # to match). `used` returns only the bands carrying a real cut.
        for band in range(1, 7):
            self.con.set_bus_eq(band, 1000.0, 0.0, q=2.0)
        used: set[int] = set()
        for s in self.plan(res):
            self.con.set_bus_eq(s["band"], s["hz"], s["gain"], q=s["q"])
            used.add(s["band"])
            if s["kind"] == "room":
                log(f"bus EQ {s['band']}: {s['hz']:.0f} Hz {s['gain']:+.1f} dB")
            else:
                log(f"pre-dip {s['band']}: {s['hz']:.0f} Hz {s['gain']:+.1f} dB "
                    f"(excess {s['excess']:.1f} dB)")
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
