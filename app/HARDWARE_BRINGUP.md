# Hardware bring-up & validation checklist

The software is feature-complete, but **production readiness is gated by validation
against your real M32C + audio interface** — and that can only be done with the gear
in front of you. The X32/M32 OSC scalings and the `/meters` blob layout vary by
firmware; the spec says to verify every constant with a sniffer before trusting it
live. Work through this once on the bench, then once at a real soundcheck.

Nothing here is destructive to a show: do it with the PA off or at low volume, and
keep a hand on the console — the app only ever *nudges* within guardrails and a human
takeover always wins.

---

## What the emulator already covers (and what it can't)

Before you touch hardware, `python run.py --emulate` runs the **entire** real stack
— `OscConsole` → UDP socket → a protocol-faithful desk emulator → `/meters` stream +
`/xremote` echoes → `MeterReceiver` → reconciliation, plus the real capture queue fed
by a timed audio stream. `python -m unittest discover -s tests -p test_contract.py`
then asserts the exact wire layout. Together these validate the **mechanics**: OSC
framing/encode/decode, the subscribe→renew handshake, self-move-echo vs.
reconciliation timing, scene-recall round-trips, meter-blob parsing, and the
capture/underrun/dead paths — all without gear.

What the emulator **cannot** tell you: whether those formats are *correct for your
firmware*. The emulator encodes the same assumptions the app does, so it can only
prove self-consistency. The checklist below — especially section 1 — is exactly the
set of assumptions to confirm against the real desk. Each is also a `VERIFY:` line in
`tests/test_contract.py`; once you have a capture, pin those expected values to the
real bytes and the test suite becomes a true regression gate.

---

## 0. Prep
- [ ] `pip install numpy python-osc sounddevice` (+ `cryptography qrcode` for tablet/TLS)
- [ ] Console and laptop on the same wired/Wi-Fi LAN; note the console IP.
- [ ] A sniffer ready: Wireshark (UDP 10023/10024) or the X32 OSC monitor / `OSC` debug.

## 1. OSC control scalings  (the #1 thing to verify)
For each, move it in the app and confirm the console does the **same** thing, and
move it on the console and confirm the value the app reports matches.
- [x] **Fader**: VALIDATED against **X32-Edit** (2026-06-30). The emulated desk now
      serves X32-Edit's discovery + sync, so X32-Edit renders what the app sends.
      Pushed 0/+5/−10/−20/−40 dB via `db_to_fader` → X32-Edit displayed exactly those
      dB. The linear 0..1 ↔ dB law in `osc.py` matches Behringer's. (Re-confirm on the
      real M32C, but the scaling itself is no longer an assumption.)
- [ ] **Preamp gain**: clip-trim −3 dB actually drops the head-amp ~3 dB.
- [ ] **Channel EQ notch**: a feedback notch lands on the right band, freq, Q, and gain.
- [ ] **Main-bus PEQ**: room-correction cuts hit the main/stereo bus, not a channel.
- [ ] **Mute** and **scene recall** (`/-action/goscene`) do what you expect.
> If any mapping is off, fix the scaling in `trio_mix/osc.py` (or `config.py`) — the
> code centralises every conversion so there's one place to correct per parameter.

> **Validate the app→console direction NOW, without the desk:** run `--emulate` and
> connect X32-Edit/M32-Edit to this PC (it discovers the emulated desk at the LAN IP
> on UDP 10023). Anything the app sends is rendered by Behringer's own editor, so the
> mute/EQ-notch/headamp/scene assumptions above can each be confirmed against it the
> same way the fader was. (The `/meters` ingestion in §2 is console→app, so it still
> needs the real desk.)

## 2. `/meters` ingestion + fader reconciliation
- [ ] Start the app in hardware mode; in the sniffer confirm the console is sending
      `/meters/1` blobs to UDP 10024 after the app's `/batchsubscribe` + `/xremote`.
- [ ] Confirm `metersrv.decode_meter_blob` produces sane values (0..1) — if the count
      or float layout is off for your firmware, adjust `decode_meter_blob`.
- [ ] Move a fader **on the console**; confirm the app log shows
      "moved on console → …" and auto-ride yields (the dashboard shows `console: connected`).
- [ ] Confirm the app's own ride moves do **not** show up as "moved on console"
      (self-move suppression is working).

## 3. Audio capture (the listening half)
- [ ] `python run.py --hardware --console-ip <ip> --list-devices` → find the interface
      (the console's USB/card return + the FOH measurement mic).
- [ ] Launch with `--audio-device <n>`; confirm `capture: {kind: audio, dead: false}`
      and that channel meters move with real input.
- [ ] Verify the **channel map**: console channel N must map to the device channel the
      app reads for role N. Default is N→N−1; set `channel_map` in `run.py`/`SoundDeviceCapture`
      if your routing differs. Speak into the lead mic → the *lead* meter moves.
- [ ] Confirm the **measurement mic** is on `MEAS_MIC_CH` (config) and hears the room.
- [ ] Confirm the startup log prints `listening on '<your interface>'` (catches a
      wrong/shifted device index) and the footer chip reads `audio: … ok`.
- [ ] **macOS:** if you ever clicked "Don't Allow" on the mic prompt, the footer chip
      shows **SILENT** and a banner says "inputs are silent — check Microphone
      permission". Fix in System Settings → Privacy → Microphone.
- [ ] **Unplug test:** pull the interface briefly → a CRITICAL "deaf" banner appears;
      re-plug → the app auto-recovers within a few seconds ("audio capture recovered"
      in the log, banner clears). No restart needed.

## 4. Calibration (pink noise)
- [ ] PA at a safe level. Run calibration; confirm pink noise actually plays out the PA
      and the meas mic records it (watch for the "heard near-silence" abort — that means
      playback or the mic isn't reaching the analyzer).
- [ ] Confirm the resulting cuts land on the main bus and sound sensible (no wild EQ).

## 5. Show clock → scene recall  (if using AbleSet)
- [ ] Point AbleSet's OSC output at the app: `--ableset-port <p>`.
- [ ] In the sniffer, confirm AbleSet sends `/setlist/activeSongName` etc. If the
      addresses differ on your AbleSet version, adjust the `disp.map(...)` lines in
      `showclock.AbleSetReceiver.start`.
- [ ] Change songs in AbleSet → the app recalls the template's scene and the song bar
      updates. Verify scene numbers in your `--template` match the console's scenes.

## 6. Latency / success criteria
- [ ] Watch the footer `loop … ms · jit … ms`. Reflex actions (feedback/clip) should be
      well under the spec's targets; investigate if `tick_ms` approaches the audio block
      period or jitter is large (CPU contention, too small a `--audio-device` buffer).

## 7. First real show — safety-net only
- [ ] Run with automatic **jobs disabled**; watch the decision log to confirm what it
      *would* do is correct, before enabling anything.
- [ ] Enable feedback + clip protection first; add vocal ride / balance once trusted.
- [ ] Keep TAKEOVER one tap away. Review the SQLite session log afterwards
      (`sessions.db`) and tag the venue (`--venue`) to start building venue history.

---

## Known limitations (by design — plan around these)

- **One input device must expose all channels.** The listening half opens a single
  audio device for the console returns **and** the measurement mic. If your meas mic
  is on a *separate* interface, combine them first: macOS → Audio MIDI Setup
  **Aggregate Device**; Windows → an ASIO aggregate (e.g. ASIO4ALL) or a single
  multichannel interface. A device with too few channels is rejected at launch with a
  clear message.
- **48 kHz, float32.** Capture runs at 48 kHz. A device locked to 44.1 kHz is rejected
  with a clear message (no auto-resample) — set the interface to 48 kHz.
- **Calibration output.** Pink noise plays to the OS default output unless you pass
  `--output-device` (point it at the PA) — otherwise it may play out the laptop
  speakers and calibration will (correctly) abort on "near-silence".
- **The emulator validates mechanics, not your firmware's exact OSC formats** — that's
  what §1 is for.
- **Channel EQ shares 4 bands.** The X32/M32 input has 4 PEQ bands; the performer EQ
  view and the automatic feedback notcher use the *same* 4 bands. The EQ view shows
  active notches as dashed markers + a note, but an operator edit and an auto-notch
  can land on the same band — the operator (or TAKEOVER) wins. The feedback FX/aux
  send OSC addresses are tagged `VERIFY:` like the rest.
- **TAKEOVER holds the *automatic* system, not your hands.** Takeover mutes the main
  and freezes all automatic jobs + scene recall + calibration; the manual surface
  (faders, mutes, EQ, FX, sends from the dashboard/iPad) stays live so you can fix
  things by hand. Quitting the app leaves the console untouched.

The app now actively detects and surfaces the rest (denied mic permission, a mid-show
unplug + auto-recovery, xruns, a busy/late device that recovers without a relaunch,
port clashes, a lost console feed). See RUNBOOK.md §10.

---

When every box is checked on your console, the hardware path is validated and the
system is production-ready for that rig. Re-verify section 1 after any console
firmware update.
