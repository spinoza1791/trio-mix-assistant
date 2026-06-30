"""Show template — the per-show configuration the engine builds its model from.

A small, validated JSON document: a list of songs, each mapping a song name (as
AbleSet reports it) to a console scene and the per-song reference levels the
assistant should hold (lead-vocal target, balance targets). Loaded at show time;
a sensible default (the acoustic trio) is embedded so the app runs with no file.

Kept to stdlib (dataclasses + explicit validation) so there's no hard pydantic
dependency; the validation is strict and raises a clear error on a bad file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import config as C


class TemplateError(ValueError):
    """A show-template file failed validation."""


@dataclass
class Song:
    name: str
    scene: int | None = None                 # console scene index to recall (None = leave)
    lead_target: float | None = None         # per-song lead-vocal loudness target (dB)
    balance: dict[int, float] = field(default_factory=dict)   # ch -> hold target (dB)
    guest: bool = False                      # activate guest channels for this song


@dataclass
class ShowTemplate:
    name: str = "Acoustic Trio"
    songs: list[Song] = field(default_factory=list)
    channels: dict | None = None         # optional channel-map spec (see config.apply_channel_map)

    def song_by_name(self, name: str) -> Song | None:
        if not name:
            return None
        nl = name.strip().lower()
        for s in self.songs:
            if s.name.strip().lower() == nl:
                return s
        return None

    def song_at(self, index: int) -> Song | None:
        return self.songs[index] if 0 <= index < len(self.songs) else None


# ---------------------------------------------------------------------------
# Validation + loading
# ---------------------------------------------------------------------------
def _num_or_none(v, ctx):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TemplateError(f"{ctx}: expected a number, got {v!r}")
    return float(v)


def from_dict(d: dict) -> ShowTemplate:
    if not isinstance(d, dict):
        raise TemplateError("template must be a JSON object")
    channels = _parse_channels(d.get("channels"))
    # validate song balance against the template's OWN map if it defines one,
    # else the active config map
    valid_ch = set(int(k) for k in channels["map"]) if channels and channels.get("map") \
        else set(C.CHANNELS)
    songs_raw = d.get("songs", [])
    if not isinstance(songs_raw, list):
        raise TemplateError("'songs' must be a list")
    if len(songs_raw) > 1000:
        raise TemplateError("too many songs (max 1000)")
    songs: list[Song] = []
    seen = set()
    for i, sd in enumerate(songs_raw):
        if not isinstance(sd, dict):
            raise TemplateError(f"songs[{i}] must be an object")
        name = sd.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TemplateError(f"songs[{i}].name is required (non-empty string)")
        key = name.strip().lower()
        if key in seen:
            raise TemplateError(f"duplicate song name: {name!r}")
        seen.add(key)
        scene = sd.get("scene")
        if scene is not None and (isinstance(scene, bool) or not isinstance(scene, int)):
            raise TemplateError(f"songs[{i}].scene must be an int or omitted")
        if scene is not None and not (0 <= scene <= 99):     # X32/M32 scene range
            raise TemplateError(f"songs[{i}].scene {scene} out of range (0-99)")
        bal_raw = sd.get("balance", {}) or {}
        if not isinstance(bal_raw, dict):
            raise TemplateError(f"songs[{i}].balance must be an object")
        balance = {}
        for ch_s, lvl in bal_raw.items():
            try:
                ch = int(ch_s)
            except (TypeError, ValueError):
                raise TemplateError(f"songs[{i}].balance key {ch_s!r} is not a channel number")
            if ch not in valid_ch:
                raise TemplateError(f"songs[{i}].balance: channel {ch} is not in the channel map")
            balance[ch] = _num_or_none(lvl, f"songs[{i}].balance[{ch}]")
        songs.append(Song(
            name=name.strip(),
            scene=scene,
            lead_target=_num_or_none(sd.get("lead_target"), f"songs[{i}].lead_target"),
            balance=balance,
            guest=bool(sd.get("guest", False)),
        ))
    return ShowTemplate(name=str(d.get("name", "Show")), songs=songs, channels=channels)


def _parse_channels(spec) -> dict | None:
    """Validate the optional 'channels' block (the template-driven map)."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise TemplateError("'channels' must be an object")
    m = spec.get("map")
    if m is not None:
        if not isinstance(m, dict) or not m:
            raise TemplateError("channels.map must be a non-empty object")
        for k, v in m.items():
            try:
                int(k)
            except (TypeError, ValueError):
                raise TemplateError(f"channels.map key {k!r} is not a channel number")
            if not isinstance(v, str) or not v.strip():
                raise TemplateError(f"channels.map[{k}] must be a non-empty role name")
        nums = set(int(k) for k in m)
    else:
        nums = None
    for key in ("lead", "meas_mic", "stage_mic"):
        v = spec.get(key)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, int):
                raise TemplateError(f"channels.{key} must be an int")
            if nums is not None and v not in nums:
                raise TemplateError(f"channels.{key}={v} is not in channels.map")
    for key in ("balance", "guest"):
        seq = spec.get(key)
        if seq is not None:
            if not isinstance(seq, list):
                raise TemplateError(f"channels.{key} must be a list")
            for x in seq:
                if nums is not None and int(x) not in nums:
                    raise TemplateError(f"channels.{key} channel {x} is not in channels.map")
    links = spec.get("stereo_links")
    if links is not None:
        if not isinstance(links, list):
            raise TemplateError("channels.stereo_links must be a list of pairs")
        for pair in links:
            if not isinstance(pair, list) or len(pair) != 2:
                raise TemplateError("channels.stereo_links entries must be [a, b] pairs")
            if nums is not None and (int(pair[0]) not in nums or int(pair[1]) not in nums):
                raise TemplateError(f"channels.stereo_links {pair} not in channels.map")
    return spec


def load(path: str) -> ShowTemplate:
    with open(path, "r", encoding="utf-8") as f:
        return from_dict(json.load(f))


def default_template() -> ShowTemplate:
    """Embedded trio setlist so the app (and the simulator) has songs + scenes
    to transition through with no external file."""
    return from_dict({
        "name": "Acoustic Trio — demo set",
        "songs": [
            {"name": "Gravity",   "scene": 1, "lead_target": -6.0},
            {"name": "Bloom",     "scene": 2, "lead_target": -7.0,
             "balance": {"5": -21.0, "7": -26.0}},
            {"name": "Wildflower","scene": 3, "lead_target": -5.0},
            {"name": "Tide",      "scene": 4, "lead_target": -6.5, "guest": True},
        ],
    })
