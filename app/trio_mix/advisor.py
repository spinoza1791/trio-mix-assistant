"""The slow advisory layer (AutoFOH Phase 5).

A latency-tolerant background worker that periodically reads the engine's recent
context and asks Claude for a short, plain-language note for the musicians or the
operator ("guitar is masking the vocal — ease off a touch"). It is ADVISORY ONLY:
it never moves a fader, gain, or EQ — the deterministic engine owns all real-time
control. This separation is the whole point of the fast/slow split in the spec.

Runs entirely off the real-time path: it calls `get_context()` (which briefly
takes the engine lock), then does the network call OFF-lock, then `on_advice()`.
Degrades gracefully: with no API key (and no injected caller) it simply doesn't
start, and the rest of the app is unaffected. The HTTP call uses urllib so there
is no hard dependency on the anthropic SDK; tests inject a fake `caller`.
"""
from __future__ import annotations

import json
import os
import re
import threading

SYSTEM_PROMPT = (
    "You are the slow advisory layer of an automatic live-sound assistant for an "
    "acoustic trio (lead vocal, harmonies, acoustic guitar, bass, cajon, keys). A "
    "separate deterministic system already handles ALL real-time safety — feedback "
    "notching, clip protection, vocal-level riding, room calibration. You do NOT "
    "control anything and must NEVER instruct fader/gain/EQ/mute/scene changes. "
    "Read the recent event context and produce a SHORT, plain-language note for the "
    "musicians or the front-of-house operator. Reply with STRICT JSON ONLY, no prose:\n"
    '{"performer_prompt": "<=120 chars, empty string if nothing needs attention>", '
    '"room_assessment": "<=160 chars on the room/feedback trend>", '
    '"severity": "info|notice|warn"}'
)


class Advisor:
    def __init__(self, get_context, on_advice, api_key: str | None = None,
                 model: str = "claude-haiku-4-5-20251001", interval: float = 30.0,
                 caller=None) -> None:
        self.get_context = get_context
        self.on_advice = on_advice
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.interval = interval
        self._injected = caller is not None
        self.caller = caller or self._http_call
        self.available = self._injected or bool(self.api_key)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.calls = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        if not self.available or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._loop, name="advisor", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _loop(self) -> None:
        # wait one interval first so there's some context to assess
        while not self._stop.wait(self.interval):
            self.tick()

    def tick(self) -> dict | None:
        """One advisory cycle (also callable directly in tests)."""
        try:
            ctx = self.get_context()
            advice = self.caller(ctx)
            self.calls += 1
            if isinstance(advice, dict):
                self.on_advice(advice)
                return advice
        except Exception as exc:                  # network / parse / API error
            self.last_error = f"{type(exc).__name__}: {exc}"
        return None

    # -- default network caller (Anthropic Messages API via urllib) --------
    def _http_call(self, ctx: dict) -> dict | None:   # pragma: no cover - needs network
        import urllib.request
        body = {
            "model": self.model,
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(ctx)[:6000]}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json",
                     "x-api-key": self.api_key or "",
                     "anthropic-version": "2023-06-01"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        text = "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text")
        return self.parse(text)

    @staticmethod
    def parse(text: str) -> dict | None:
        """Pull the JSON advisory object out of a model response, defensively."""
        if not text:
            return None
        text = text[:8000]                       # bound before the regex (hostile input)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        sev = str(d.get("severity", "info")).strip().lower()
        if sev not in ("info", "notice", "warn"):
            sev = "info"
        return {"performer_prompt": str(d.get("performer_prompt", "")).strip()[:200],
                "room_assessment": str(d.get("room_assessment", "")).strip()[:240],
                "severity": sev}
