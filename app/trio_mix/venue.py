"""Venue learning (AutoFOH Phase 5).

Post-show, the session log is mined for the frequencies that actually fed back in
a given room. Those recurring freqs are written to a per-venue model JSON; next
time you load that venue, they pre-seed the assistant's watch-list so feedback at
the room's known problem frequencies is caught a block sooner — the system
"improves with use". A confidence score scales with how many shows back the model.

This is deliberately conservative: it only *seeds the watch-list* (which makes the
existing, guard-railed feedback detector react sooner), never auto-applies cuts or
overrides calibration. It's a prior, not an action.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

_FREQ_RE = re.compile(r"(\d+)\s*Hz")


def slug(venue: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (venue or "").strip().lower()).strip("-")
    return s or "venue"


@dataclass
class VenueModel:
    venue: str = ""
    shows: int = 0
    feedback_freqs: list = field(default_factory=list)   # [{"hz": int, "count": int}]
    confidence: float = 0.0                               # 0..1, scales with show count
    updated: float = 0.0

    def watch_freqs(self, min_count: int = 2) -> list[float]:
        """Freqs that recurred at least `min_count` times (worth pre-watching)."""
        return [float(f["hz"]) for f in self.feedback_freqs if f["count"] >= min_count]


def build_model(session_log, venue: str, now: float | None = None) -> VenueModel:
    """Mine the session log for a venue's recurring feedback frequencies."""
    msgs = session_log.venue_feedback(venue)
    shows = session_log.venue_shows(venue)
    bins: Counter = Counter()
    members: dict[int, list[int]] = {}
    for m in msgs:
        mm = _FREQ_RE.search(m or "")
        if not mm or len(mm.group(1)) > 6:      # ignore absurd/huge "NNN Hz" (corrupt DB)
            continue
        hz = int(mm.group(1))
        if hz < 20 or hz > 24000:
            continue
        b = round(math.log2(hz) * 6)            # 1/6-octave bins merge near-duplicates
        bins[b] += 1
        members.setdefault(b, []).append(hz)
    feedback_freqs = []
    for b, count in bins.most_common(8):
        hzs = sorted(members[b])
        feedback_freqs.append({"hz": int(hzs[len(hzs) // 2]), "count": count})   # bin median
    confidence = round(min(1.0, shows / 3.0), 2)          # ~3 shows -> full confidence
    return VenueModel(venue=venue, shows=shows, feedback_freqs=feedback_freqs,
                      confidence=confidence, updated=(now or 0.0))


def model_path(venue_dir: str, venue: str) -> str:
    return os.path.join(venue_dir, slug(venue) + ".json")


def save_model(model: VenueModel, venue_dir: str) -> str:
    os.makedirs(venue_dir, exist_ok=True)
    path = model_path(venue_dir, model.venue)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(model), f, indent=2)
    return path


def load_model(venue: str, venue_dir: str) -> VenueModel | None:
    path = model_path(venue_dir, venue)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return VenueModel(venue=d.get("venue", venue), shows=int(d.get("shows", 0)),
                          feedback_freqs=list(d.get("feedback_freqs", [])),
                          confidence=float(d.get("confidence", 0.0)),
                          updated=float(d.get("updated", 0.0)))
    except (OSError, ValueError, TypeError):
        return None
