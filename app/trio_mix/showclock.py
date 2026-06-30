"""Show clock — where the system is in the set, so scenes recall automatically.

Two sources, same `on_change(ShowState)` contract:

  * SimShowClock     — cycles the template's setlist on a timer (no external app),
                       so the simulator demonstrates song→scene transitions.
  * AbleSetReceiver  — listens for AbleSet's OSC messages (song/section/playback)
                       over UDP, exactly like the meter receiver. AbleSet is the
                       band's backing-track/lyrics clock; when the song changes it
                       pushes the new song name + index, and we recall the scene.

The exact AbleSet OSC addresses are firmware/version-specific — verify them
against your AbleSet with an OSC monitor (same caveat as the console scalings).
The handlers below target AbleSet's documented `/setlist` + `/playback` API.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from . import config as C


@dataclass
class ShowState:
    song_index: int | None = None
    song_name: str = ""
    next_song: str = ""
    section: str = ""
    bpm: float | None = None
    playing: bool = False

    def key(self) -> tuple:
        # what counts as a "change worth acting on"
        return (self.song_name, self.section, self.playing)


class ShowClock:
    """Base: holds an on_change callback and a current ShowState."""

    def __init__(self, on_change=None) -> None:
        self.on_change = on_change
        self.state = ShowState()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def _fire(self, state: ShowState) -> None:
        self.state = state
        if self.on_change:
            self.on_change(state)


# ---------------------------------------------------------------------------
# Simulated clock: walk the setlist on a timer
# ---------------------------------------------------------------------------
class SimShowClock(ShowClock):
    def __init__(self, template, on_change=None, interval: float = 15.0) -> None:
        super().__init__(on_change)
        self.template = template
        self.interval = interval
        self._i = -1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _advance(self) -> None:
        songs = self.template.songs
        if not songs:
            return
        self._i = (self._i + 1) % len(songs)
        cur = songs[self._i]
        nxt = songs[(self._i + 1) % len(songs)]
        self._fire(ShowState(song_index=self._i, song_name=cur.name,
                             next_song=nxt.name, section="Song", bpm=None, playing=True))

    def _loop(self) -> None:
        self._advance()                                  # kick off on song 1 immediately
        while not self._stop.wait(self.interval):
            self._advance()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="showclock", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ---------------------------------------------------------------------------
# AbleSet clock: receive song/section over OSC (UDP)
# ---------------------------------------------------------------------------
def _as_str(args) -> str:
    return str(args[0]).strip() if args else ""


def _as_float(args):
    try:
        return float(args[0])
    except (TypeError, ValueError, IndexError):
        return None


class AbleSetReceiver(ShowClock):
    """Bind a UDP port and translate AbleSet's OSC pushes into ShowState changes.
    Fires on_change only when the song / section / playback actually changes (not
    on every beat tick)."""

    def __init__(self, on_change=None, ip: str = "0.0.0.0",
                 port: int = C.ABLESET_PORT) -> None:
        super().__init__(on_change)
        self.ip, self.port = ip, port
        self.server = None
        self._running = False
        self._cur = ShowState()
        self._last_key = None

    # -- handlers ----------------------------------------------------------
    def _maybe_fire(self) -> None:
        if not self._running:
            return
        k = self._cur.key()
        if k != self._last_key and self._cur.song_name:
            self._last_key = k
            # copy so a later in-place edit can't mutate what we handed out
            self._fire(ShowState(**vars(self._cur)))

    def h_song_name(self, address, *args) -> None:
        if not self._running:
            return
        self._cur.song_name = _as_str(args)
        self._maybe_fire()

    def h_song_index(self, address, *args) -> None:
        if not self._running:
            return
        v = _as_float(args)
        self._cur.song_index = int(v) if v is not None else None

    def h_next_song(self, address, *args) -> None:
        if not self._running:
            return
        self._cur.next_song = _as_str(args)

    def h_section(self, address, *args) -> None:
        if not self._running:
            return
        self._cur.section = _as_str(args)
        self._maybe_fire()

    def h_playing(self, address, *args) -> None:
        if not self._running:
            return
        v = _as_float(args)
        self._cur.playing = bool(v) if v is not None else self._cur.playing
        self._maybe_fire()

    def h_bpm(self, address, *args) -> None:
        if not self._running:
            return
        self._cur.bpm = _as_float(args)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
        disp = Dispatcher()
        disp.map("/setlist/activeSongName", self.h_song_name)
        disp.map("/setlist/activeSongIndex", self.h_song_index)
        disp.map("/setlist/nextSongName", self.h_next_song)
        disp.map("/song/sectionName", self.h_section)
        disp.map("/playback/isPlaying", self.h_playing)
        disp.map("/song/beatsPerMinute", self.h_bpm)
        disp.set_default_handler(lambda *a: None)
        self.server = ThreadingOSCUDPServer((self.ip, self.port), disp)
        self._running = True
        threading.Thread(target=self.server.serve_forever, name="ableset",
                         daemon=True).start()

    def stop(self) -> None:
        self._running = False
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
