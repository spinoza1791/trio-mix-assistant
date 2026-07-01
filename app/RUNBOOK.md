# Runbook — Acoustic Trio AI Mix‑Assistant (Windows & macOS)

A practical, show‑day operating guide for the FOH laptop. The app is the same on
both OSes (pure Python + numpy + python‑osc + sounddevice); only the shell syntax
and a few OS permissions differ. Where a step is identical, it's written once;
where it differs, there's a **Windows** and a **macOS** version side by side.

> Conventions: lines starting `PS>` are **Windows PowerShell**; lines starting
> `$` are **macOS Terminal** (zsh/bash). PowerShell continues lines with a
> backtick `` ` ``; macOS continues with a backslash `\`.

---

## 0. Mental model (read once)

- **The console is the source of truth.** The app *listens* (channel audio + a FOH
  measurement mic) and *nudges* the desk within hard guardrails. It never owns the mix.
- **A human always wins.** Move a fader on the desk and the app adopts it (auto‑ride
  yields); tap **TAKEOVER** and it mutes main + freezes every automatic move.
- **The AI advisor (optional) never touches the mix** — it only posts text suggestions.
- **First time on a given console you must validate the OSC formats** against that
  firmware (see `HARDWARE_BRINGUP.md`). After that passes once, every show is just the
  launch command in §3.

---

## 1. Install (one time per laptop)

You need Python 3.10+.

### Windows
```powershell
PS> python --version            # if missing, install from python.org and re-open the terminal
PS> python -m pip install numpy python-osc sounddevice cryptography qrcode
```
If `python` isn't found but `py` is, use `py -m pip ...` and `py run.py ...`.

### macOS
```bash
$ python3 --version             # if missing: brew install python  (or python.org)
$ pip3 install numpy python-osc sounddevice cryptography qrcode
```
If `import sounddevice` later fails, install PortAudio and reinstall:
`brew install portaudio && pip3 install --force-reinstall sounddevice`.

> `numpy` alone is enough for **simulation/rehearsal**. `python-osc` + `sounddevice`
> are only needed to drive real hardware. `cryptography`/`qrcode` are optional niceties
> (`--https` and the scan‑to‑open QR).

---

## 2. First‑time rig setup (once per console + interface)

Do this with the **PA off or at low volume**, hand on the console.

### 2.1 Put everything on one network
- Console, FOH laptop (and tablet if used) on the **same LAN/subnet**, wired or a
  dedicated Wi‑Fi AP. Note the **console IP**.

### 2.2 Find the audio interface
```powershell
PS> python run.py --hardware --console-ip 192.168.1.50 --list-devices
```
```bash
$ python3 run.py --hardware --console-ip 192.168.1.50 --list-devices
```
Note the **index** of the input device carrying the console's USB/card return **and**
your measurement mic.

- **macOS multichannel:** if the console return and the meas mic are *separate*
  devices, combine them in **Audio MIDI Setup → "+" → Create Aggregate Device**, then
  use that aggregate's index. (Same interface for both = nothing to do.)
- **Windows multichannel:** prefer the interface's **ASIO** or WASAPI device that
  exposes all input channels at once.

### 2.3 Grant OS permissions (first launch will need these)
- **macOS — Microphone (critical):** the first time, **System Settings → Privacy &
  Security → Microphone → enable Terminal** (or your IDE). If denied, capture is
  *silent*, the assistant is deaf, and calibration aborts with "heard near‑silence".
- **macOS — Network:** when you launch with `--lan`, click **Allow** on the "accept
  incoming connections?" prompt.
- **Windows — Firewall:** the first `--lan` launch pops **Windows Defender Firewall** —
  check **Private networks → Allow access** (otherwise the tablet can't connect, and
  the console's meter replies on UDP may be blocked).

### 2.4 Validate the OSC contract (the real gate)
Walk **`HARDWARE_BRINGUP.md` §1–§5** with a sniffer (Wireshark / X32 OSC monitor):
confirm fader/gain/EQ scalings, the `/meters` decode, scene recall, and the channel
map. Speak into the lead mic → the **lead** meter (not another) must move. This is the
one step the emulator can't do for you. Done once per firmware.

> Want to rehearse the whole flow *before* the gear arrives? `python run.py --emulate`
> runs the identical stack against a built‑in desk emulator (see §8).

---

## 3. Show‑day launch

### Windows
```powershell
PS> python run.py --hardware `
      --console-ip 192.168.1.50 `
      --audio-device 2 `
      --template myset.json `
      --venue "The Cellar" `
      --lan --https
```

### macOS
```bash
$ python3 run.py --hardware \
      --console-ip 192.168.1.50 \
      --audio-device 2 \
      --template myset.json \
      --venue "The Cellar" \
      --lan --https
```

Optional add‑ons (either OS):
- `--ableset-port 39051` — AbleSet drives automatic scene recall.
- `--advisor` — the Claude advisory layer. Set the key first:
  - Windows: `PS> $env:ANTHROPIC_API_KEY="sk-..."`
  - macOS: `$ export ANTHROPIC_API_KEY=sk-...`
- Drop `--audio-device` to run **manual mixing + scene recall only** (assistant idle).
- Drop `--https` if you don't need tablet Wake Lock (plain http is fine on the laptop).

On launch it prints the dashboard URL (and a scan‑to‑open QR when `--lan`).

### 3.1 Verify in the first 30 seconds (on the dashboard)
| Check | Good value |
|---|---|
| Mode badge | **HARDWARE** |
| Status pill | **AUTO** (green) — not **ALERT** |
| Footer audio chip | `audio: <your interface> ok` (not SILENT / DEAD / XRUN) |
| **console feed** | `connected: true`, `meters > 0` |
| Footer latency | `loop … ms` small, stable |

The app actively watches the hardware and raises an **ALERT banner** for the common
problems, so you don't have to read gauges:
- *"inputs are silent — check Microphone permission…"* → on macOS this is almost always
  a denied **Microphone** permission; also check input gain and the patch.
- *"audio capture stopped — the assistant is deaf"* → the interface was unplugged/lost;
  re-connect it and the app **auto-recovers** (the banner clears, "audio capture
  recovered" appears in the log).
- *"no console feed … — check the console IP, network, and firewall"* → OSC isn't
  arriving; see §10.
- *"audio interface is dropping samples (xruns)…"* → raise the device buffer size.

The startup log also prints **`listening on '<device name>'`** — confirm it's the right
interface (catches a shifted device index). **Don't go live while the pill is ALERT.**

---

## 4. Soundcheck sequence (build trust gradually)

1. **Calibrate the room** — PA at show level, room quiet → *Run calibration*. Watch for
   the "heard near‑silence" abort (means pink noise or the meas mic isn't reaching the
   analyzer — fix before continuing).
2. **Enable the safety‑net first:** *feedback catch* + *clip protection*. Watch the
   **decision log** — confirm what it does is correct before adding more.
3. **Add vocal ride**, then **balance** (set your levels, tap *Capture balance now*).
4. **Load the set:** confirm the song bar shows the right song/scene; tap a scene in the
   **Scenes** panel to test manual recall.
5. Keep **TAKEOVER** within reach.

---

## 5. Tablet (iPad / Android) — optional

Launched with `--lan` (and ideally `--https`):
1. On the tablet, open the printed LAN URL or **scan the QR** in the terminal.
2. `--https` is self‑signed → **accept the certificate warning once**.
3. **Add to Home Screen** → it installs as a full‑screen PWA with screen Wake Lock.
4. Same network only; if the AP has "client isolation"/guest mode, turn it off.

---

## 6. During the show

- **Screens** (tabs at the top): **Mix** (faders + jobs + log, the 95% view) · **EQ**
  (per-channel 4-band PEQ with a live curve; dashed markers = the assistant's auto
  notches) · **FX** (per-channel sends + wet returns) · **Settings** (connection,
  latency, room SNR, venue model).
- **Notifications, 4 levels:** silent auto-correct → log entry → dismissable **banner**
  (warn) → full-screen **acknowledge overlay** (critical). No non-critical pop-ups in
  the **first 8 bars** of a song.
- **Operator overrides win:** a console/iPad fader move is adopted automatically; the
  app stops riding that channel briefly. EQ/FX/fader edits stay live even under takeover.
- **COACH** (header button): flips the assistant from *acting* to *advising*. It detects
  the same feedback/clip/level problems but, instead of touching the console, lists the
  exact manual move — channel, band, dB, Hz — in a **Coach — manual moves** panel and the
  decision log (e.g. *"Feedback 2.5 kHz in the room → cut the MAIN BUS at 2500 Hz by −9 dB
  (Q 8)"*). Zero console writes, no AI — the numbers are the same deterministic math the
  automatic jobs would use. The status pill reads **COACH**. Use it to mix by hand with
  the app as a guide, or to see what it *would* do before trusting a job.
- **TAKEOVER** = panic: mutes main, freezes all automatic jobs + scene recall +
  calibration. Tap again to release.
- **AI advisor** (if enabled) posts short suggestions in its card — they are *advice*,
  never actions.

---

## 7. After the show

- Every decision is in **`sessions.db`** (SQLite), tagged with `--venue`. Over multiple
  shows at the same room this builds venue history (the basis for venue learning).
- `--session-db off` disables logging; `--session-db path\to\file.db` redirects it.

---

## 8. Rehearse with no hardware (`--emulate`)

Practice the exact launch + dashboard with zero risk:
```powershell
PS> python run.py --emulate --lan --https
```
```bash
$ python3 run.py --emulate --lan --https
```
This runs the **full real stack** (OSC over localhost) against a built‑in
protocol‑faithful desk emulator + a timed audio feed: meters stream, feedback gets
caught, faders reconcile — same dashboard, same muscle memory. (It validates mechanics,
not your firmware's exact formats — that's still §2.4.)

---

## 9. Command reference

| Flag | Meaning |
|---|---|
| `--hardware` | drive a real M32C over OSC |
| `--emulate` | full stack against the built‑in desk emulator (no gear) |
| `--console-ip <ip>` | the M32C's IP (hardware mode) |
| `--audio-device <n>` | input device index/name for the listening half |
| `--output-device <n>` | output device the calibration pink noise plays to (point at the PA) |
| `--auto` | auto-detect the best input device, size the channel map to it, auto-set input gain |
| `--input-gain <dB>` | digital input boost for a quiet mic/interface (try 20–40) |
| `--channel-map "1:0,…"` | explicit console-channel:device-column map (console 1-based, column 0-based); default N→N−1 |
| `--list-devices` | print input devices (with host API) and exit |
| `--template <file.json>` | per‑song scenes + reference levels, **and an optional channel map** (e.g. `templates/autofoh_pilot.json` = a 13‑input rig); else the built‑in 8‑ch trio |
| `--ableset-port <p>` | listen for AbleSet song/section OSC → auto scene recall |
| `--no-show-clock` | disable the simulated show clock (sim only) |
| `--venue "<name>"` | tag the session log **and load/learn this venue's model** (recurring feedback freqs pre‑seed the watch‑list; the model updates after each show) |
| `--session-db <path｜off>` | session‑log location, or disable |
| `--advisor` | enable the Claude advisory layer (needs `ANTHROPIC_API_KEY`) |
| `--advisor-interval <s>` | seconds between advisor checks (default 30) |
| `--lan` | bind to the LAN so tablets can connect |
| `--https` | serve TLS (self‑signed) for PWA install + Wake Lock |
| `--host <ip>` / `--port <n>` | bind address / port (default 8770) |

Default (no flags) = **simulation** on `http://127.0.0.1:8770/`.

---

## 10. Troubleshooting

The dashboard now names most of these in an **ALERT banner** — this table is the fix.

| Symptom (banner / message) | Likely cause → fix |
|---|---|
| **"no console feed … check the console IP, network, and firewall"** | Wrong `--console-ip`, console on a different subnet, or firewall blocking UDP. Ping the console; allow Python through the firewall (Win §2.3); confirm `/xremote` with a sniffer. Recovers automatically when the feed returns. |
| **"inputs are silent" / footer chip = SILENT** | **macOS Microphone permission denied** (System Settings → Privacy → Microphone → enable Terminal) — the #1 cause; also input gain too low, wrong device, or input muted at the desk. |
| **"audio capture stopped — the assistant is deaf" / chip = DEAD** | Interface unplugged or driver wedged. Re‑connect it — the app **auto‑retries every 3 s** and clears the banner ("audio capture recovered"). |
| **"audio … dropping samples (xruns)" / chip = XRUN** | Device buffer too small / CPU contention. Raise the interface buffer size; close other audio apps. |
| **"audio capture off — … has N input channels but needs M"** | The selected device has too few inputs. Pick a fuller interface, or make a macOS Aggregate Device (§2.2). |
| **"audio capture off — … could not be opened … in use by another app"** | Another app (DAW) holds the device (Windows ASIO/WASAPI‑exclusive). Close it, or pick a different device. |
| **"Could not open the dashboard on port 8770"** | A second copy is already running. Stop it, or launch with a different `--port`. |
| **"meter port busy" / "Desk emulator could not bind…"** | Two copies running. Stop the other instance. |
| **calibration "heard near‑silence — the test tone could not be played"** | No/locked **output** device for the pink noise — check the PA feed/output selection. |
| **Tablet can't open the page** | Not launched with `--lan`; firewall blocking inbound; different Wi‑Fi/VLAN; AP "client isolation" on. Use the exact printed URL/QR. |
| **HTTPS cert warning on tablet** | Expected (self‑signed) — accept once, then Add to Home Screen. |
| **Fader jumps / wrong EQ band on the real desk** | OSC scaling mismatch for your firmware → `HARDWARE_BRINGUP.md` §1; fix the constant in `trio_mix/osc.py`. |
| **Wrong meter moves when you play an input** | Channel‑map mismatch — the startup log shows `listening on '<device>'`; verify the patch order (§2.2) and the `channel_map`. |
| **`python` not found** | Windows: `py run.py …`. macOS: `python3 run.py …`. |
| **`import sounddevice` fails** | macOS: `brew install portaudio` then reinstall sounddevice. Windows: reinstall `sounddevice`. |
| **Audio device index changed between shows** | Indexes shift when devices are added/removed — the startup log prints the resolved name; prefer the device **name** in `show.conf` over the index. |

---

*The console is the source of truth; the app only listens & nudges within hard
guardrails; a human can always take over.*
