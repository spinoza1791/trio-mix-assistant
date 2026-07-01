# Optimal listening hardware & system requirements

**What to buy and how to wire it** so the app hears the room and the mix cleanly.
This is the *reference spec* for the listening rig. The two companion docs cover the
other phases:

- **`HARDWARE_BRINGUP.md`** — the one-time on-the-bench validation checklist (verify the
  OSC/`/meters` formats against *your* console before trusting it live).
- **`RUNBOOK.md`** — the show-day operating guide (launch commands, dashboard, tablet).

> **TL;DR — the optimal rig**
> **Console X-USB interface as the single 48 kHz capture device (WASAPI/ASIO) + an
> analog measurement mic on a spare desk channel + wired Ethernet for OSC control.**
> No outboard audio interface, no second USB mic, no sample-rate conversion.

---

## 1. Why this shape (what the software requires)

The listening half (`trio_mix/capture.py`) imposes four hard constraints. Every
recommendation below follows from them:

| Constraint | Where | Consequence for your rig |
|---|---|---|
| **One input device carries *all* channels** (console returns **and** the meas mic) | `SoundDeviceCapture` opens a single stream, `channels = max(channel_map)+1` | Use the console's own multichannel USB. A *separate* USB meas mic needs an OS aggregate device. |
| **48 kHz, float32** | `SAMPLE_RATE = 48000`; a 44.1 kHz-locked device is rejected with a clear message (no auto-resample) | Set the interface to 48 kHz; run the whole rig at 48 k. |
| **Device column = console channel − 1** (default map) | `channel_map or {ch: ch-1}` | Use the console's default X-USB routing (card sends = inputs 1–N in order), or override with `--channel-map`. |
| **Low-latency host API preferred** | ranker rewards ASIO (+4) > WASAPI / Core Audio / ALSA / JACK (+3), penalizes built-in mic arrays | Windows → **WASAPI** (or ASIO if your PortAudio build has it); macOS → **Core Audio**. |

---

## 2. Signal flow (the optimal wiring)

```
  Trio inputs ──▶ M32C preamps ──┬──▶ desk mix ──▶ mains ──▶ PA ──▶ ROOM
   (vox/gtr/…)                   │                                   │
                                 │  X-USB (32×32, 48 kHz)            │ (acoustic)
                                 ▼                                   ▼
  Analog meas mic ─▶ spare ──────┴──────▶ USB-B ─▶ FOH laptop ◀─ meas mic hears
   (EMM-6 / ECM8000)  desk ch                       │             PA + room
                                                     │
                        wired Ethernet (OSC 10023/10024)
                                                     │
                                                     ▼
                                                  M32C control
```

Two cables to the laptop: **one USB-B** (all audio, in one 48 kHz stream) and **one
Ethernet** (OSC control + `/meters`). The measurement mic is *analog into a desk
channel* so it rides the same USB stream as every other channel — it is just the last
column (`MEAS_MIC_CH`, channel 8 on the default trio map).

---

## 3. The pieces

### 3.1 Channel taps — the console's built-in USB interface
The M32C has an integrated **32×32 USB 2.0** audio interface. Route its
computer-bound sends to the **direct channel inputs** (the default X-USB routing) and
the app gets a sample-accurate digital tap of every channel — post-preamp, matching
the desk's channel numbers — with **zero extra hardware**. The desk is the 48 kHz
clock master, so there is no sample-rate conversion.

> If the USB routing sends channel *N* somewhere other than USB column *N−1*, don't
> rewire — map it: `--channel-map "1:0,2:1,…,8:9"` (console 1-based : column 0-based).

### 3.2 Measurement mic — analog, into a spare desk channel (**not** a USB mic)
This is the one non-obvious choice. Because the engine opens **one** device, a USB
measurement mic (e.g. miniDSP UMIK-1) is a *second* device and forces you into a
fragile OS aggregate. Instead use an **analog** measurement mic into a console preamp,
so it arrives on the same USB stream as `meas_mic`:

| Mic | Notes |
|---|---|
| **Dayton Audio EMM-6** | Omni, per-unit calibration file. Best value. |
| **Behringer ECM8000 / Line Audio OM1** | Omni, flat, no cal file. Fine for feedback + rough room. |
| *(avoid for this app)* miniDSP UMIK-1 | Excellent mic, but USB → second device → aggregate needed. |

**Placement:** at the mix position / a representative listening spot, on a stand at
ear height, capsule pointed up (omni), away from walls and boundaries. It is the app's
"truth about the room" — it should hear the PA + room as the audience does. A quiet
capsule is fine: `--auto` sets digital input gain automatically, or use `--input-gain
<dB>` (try 20–40 for a dynamic).

### 3.3 FOH computer, driver & buffer
- **CPU/RAM:** any modern quad-core, 8 GB+, SSD. The DSP is trivially light (a handful
  of 1024-pt FFTs per tick). The real constraints are **USB and driver stability**, not CPU.
- **Windows:** install the **Midas/Behringer X-USB driver**; run on the **WASAPI** (or
  ASIO, if present) device. The stock `sounddevice` wheel ships PortAudio **without**
  ASIO, so plan on **WASAPI exclusive** unless you build PortAudio with the ASIO SDK.
- **macOS:** **Core Audio** (class-compliant). Grant the Microphone permission on first
  launch (a denial makes capture *silent* — the app detects and banners it).
- **Buffer:** 256–512 samples @ 48 k is a good latency/stability trade. The app detects
  sustained xruns (`overload()`) and shows an **XRUN** chip — bump the buffer if you see it.
- **USB:** a short/quality USB-B cable (≤ 3 m, or an active cable) on a **dedicated**
  port — not a shared hub with other devices.

### 3.4 Control network — wired Ethernet
Put the **OSC control path** (console ↔ laptop, UDP 10023/10024) on **wired Ethernet**
with static IPs — never Wi-Fi. A dashboard **tablet** may use Wi-Fi (the dashboard is
SSE/WebSocket and tolerant); keep it on the same subnet, client-isolation off.

---

## 4. Bill of materials (tiers)

| | Channel taps | Meas mic | Interface | Network |
|---|---|---|---|---|
| **Minimum** | Console analog **direct outs** → any 8+ in USB interface | ECM8000 | class-compliant / WASAPI | Wi-Fi AP (dedicated) |
| **Recommended** | **M32C X-USB** (built-in) | EMM-6 (calibrated) | X-USB WASAPI @ 48 k | wired switch |
| **Optimal** | **M32C X-USB**, direct-input routing | EMM-6 on a dedicated preamp, at the mix position | X-USB **ASIO** @ 48 k, 256-sample buffer | dedicated wired VLAN, static IPs |

The **Minimum** row is the fallback when the console's USB can't give per-channel taps:
analog direct-outs into a separate multichannel interface. It works but adds an A/D
round-trip and hardware for no benefit over the desk's own USB — prefer X-USB.

---

## 5. System requirements (summary)

- **OS:** Windows 10/11 or macOS 12+.
- **Python:** 3.10+.
- **Packages:** `numpy` (always); `python-osc` + `sounddevice` (real hardware);
  `cryptography` + `qrcode` (optional: `--https` + scan-to-open QR).
- **Audio:** one input device exposing all channels at **48 kHz** (console X-USB).
- **Control:** IP reachability to the console on **UDP 10023/10024** (wired).

---

## 6. Validation status — read before trusting it live

The software fully supports the setup above and is hardened against the field failure
modes (unplug, silence, xrun, busy device, wrong channel count, quiet mic). **But the
happy path of real multichannel console-USB capture has not yet been run against an
actual M32C** — all multichannel testing to date is synthetic/emulated or a single USB
mic. Two things need a bench pass with the real desk to call "validated":

1. that the X-USB interface exposes N input channels under WASAPI/ASIO, and
2. that **USB column order == console channel order** (else set `--channel-map`).

That bench pass is exactly **`HARDWARE_BRINGUP.md` §3** (audio capture). The OSC
control/`/meters` side has its own gates in §1–§2 of that doc and is independent of the
audio path here.

---

*The console is the source of truth; the app only listens & nudges within hard
guardrails; a human can always take over.*
