"""Per-show session logging to SQLite — the substrate for post-show analysis and
venue learning (AutoFOH Phase 5).

The real-time engine must never block on disk, so `log_event()` only enqueues;
a background writer thread drains the queue and batches inserts. Reads (summary,
venue history) are synchronous and run off the real-time path (advisor / API).

Schema:
  sessions(id, started, venue, template, mode)
  events(session_id, t, kind, level, ch, role, msg)
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time


class SessionLog:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        # check_same_thread=False: the writer thread and reader threads share one
        # connection, serialized by self._db_lock (sqlite itself is also locked).
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._db_lock = threading.Lock()
        self._init_schema()
        self.session_id: int | None = None
        self._t0 = time.monotonic()
        self._q: queue.Queue = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._dropped = 0
        self._writer = threading.Thread(target=self._drain, name="sessionlog",
                                        daemon=True)
        self._writer.start()

    def _init_schema(self) -> None:
        with self._db_lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started REAL, venue TEXT, template TEXT, mode TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    session_id INTEGER, t REAL, kind TEXT, level TEXT,
                    ch INTEGER, role TEXT, msg TEXT);
                CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id);
                """
            )
            self._conn.commit()

    # -- session lifecycle (called at startup, off the loop) ---------------
    def start_session(self, venue: str = "", template: str = "",
                      mode: str = "") -> int:
        with self._db_lock:
            cur = self._conn.execute(
                "INSERT INTO sessions(started, venue, template, mode) VALUES (?,?,?,?)",
                (time.time(), venue, template, mode))
            self._conn.commit()
            self.session_id = int(cur.lastrowid)
        return self.session_id

    # -- hot path: enqueue only (never touches disk) -----------------------
    def log_event(self, ev: dict) -> None:
        if self.session_id is None:
            return
        try:
            self._q.put_nowait((self.session_id, round(time.monotonic() - self._t0, 2),
                                ev.get("kind"), ev.get("level"), ev.get("ch"),
                                ev.get("role"), ev.get("msg")))
        except queue.Full:
            self._dropped += 1            # best-effort logging; never block the engine

    def _drain(self) -> None:
        while not self._stop.is_set():
            batch = []
            try:
                batch.append(self._q.get(timeout=0.5))
            except queue.Empty:
                continue
            while len(batch) < 256:           # opportunistically batch
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                with self._db_lock:
                    self._conn.executemany(
                        "INSERT INTO events(session_id,t,kind,level,ch,role,msg) "
                        "VALUES (?,?,?,?,?,?,?)", batch)
                    self._conn.commit()
            except sqlite3.Error:
                pass                          # a write error must not kill the writer
            finally:
                for _ in batch:               # mark done even on error (no join leak)
                    self._q.task_done()

    # -- reads (off the real-time path) ------------------------------------
    def summary(self, session_id: int | None = None) -> dict:
        sid = session_id if session_id is not None else self.session_id
        if sid is None:
            return {}
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) FROM events WHERE session_id=? GROUP BY kind",
                (sid,)).fetchall()
        return {k: n for k, n in rows}

    def recent(self, n: int = 20, session_id: int | None = None) -> list[dict]:
        sid = session_id if session_id is not None else self.session_id
        if sid is None:
            return []
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT t, kind, level, ch, role, msg FROM events "
                "WHERE session_id=? ORDER BY rowid DESC LIMIT ?", (sid, int(n))).fetchall()
        cols = ("t", "kind", "level", "ch", "role", "msg")
        return [dict(zip(cols, r)) for r in reversed(rows)]

    def venue_feedback(self, venue: str, limit_sessions: int = 20) -> list[str]:
        """Feedback-event messages across past shows at this venue — the raw
        material for learning the room's feedback-prone frequencies."""
        with self._db_lock:
            sids = [r[0] for r in self._conn.execute(
                "SELECT id FROM sessions WHERE venue=? ORDER BY id DESC LIMIT ?",
                (venue, int(limit_sessions))).fetchall()]
            if not sids:
                return []
            ph = ",".join("?" * len(sids))
            rows = self._conn.execute(
                f"SELECT msg FROM events WHERE kind='feedback' AND session_id IN ({ph})",
                sids).fetchall()
        return [r[0] for r in rows if r[0]]

    def venue_shows(self, venue: str) -> int:
        with self._db_lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE venue=?", (venue,)).fetchone()[0])

    def venue_history(self, venue: str, limit: int = 10) -> list[dict]:
        """Past sessions at this venue with their feedback/clip counts — the raw
        material for venue learning (e.g. 'this room always rings at 2.5 kHz')."""
        with self._db_lock:
            sess = self._conn.execute(
                "SELECT id, started FROM sessions WHERE venue=? ORDER BY id DESC LIMIT ?",
                (venue, int(limit))).fetchall()
            out = []
            for sid, started in sess:
                rows = self._conn.execute(
                    "SELECT kind, COUNT(*) FROM events WHERE session_id=? GROUP BY kind",
                    (sid,)).fetchall()
                out.append({"session_id": sid, "started": started,
                            "counts": {k: n for k, n in rows}})
        return out

    def flush(self, timeout: float = 2.0) -> None:
        """Block until every enqueued event has been committed (or timeout)."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        self.flush()
        self._stop.set()
        if self._writer.is_alive():
            self._writer.join(timeout=2.0)
        with self._db_lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
