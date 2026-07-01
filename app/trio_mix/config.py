"""Rig configuration and tunable constants.

Edit these for your rig. The runtime reads CHANNELS as the single source of
truth for the channel map, so re-instrumenting the trio is a one-dict edit.
"""

# ---- Network / audio ------------------------------------------------------
CONSOLE_IP = "192.168.1.50"   # your M32C's IP (real-hardware mode only)
CONSOLE_PORT = 10023          # X32/M32 OSC port (fixed)
LOCAL_PORT = 10024            # port we listen on for /meters + replies
ABLESET_PORT = 39051          # port we listen on for AbleSet song/section OSC
SAMPLE_RATE = 48000
BLOCK = 1024                  # analysis block size (~21 ms @ 48k)

# ---- Emulated-desk identity (what X32-Edit / M32-Edit sees on connect) -----
# The discovery handshake an editor uses to recognise an X32-family console.
# VERIFY: against a real M32C /info reply (see the PGM "Unofficial X32 OSC"
# protocol + HARDWARE_BRINGUP.md). M32 is X32-protocol-compatible and reports the
# X32 model family; adjust if your desk/editor handshake differs.
X32_SERVER_VER = "V2.07"      # OSC server version string
X32_NAME = "MixAssist"        # console name shown in the editor
X32_MODEL = "X32"             # model family (X32-Edit checks this to connect)
X32_FW = "4.06-8"             # console firmware version

# ---- Channel map: console channel number -> role --------------------------
CHANNELS = {
    1: "lead_vox",
    2: "harm_vox_l",
    3: "harm_vox_r",
    4: "acoustic_gtr",
    5: "bass_di",
    6: "cajon",
    7: "keys_aux",
    8: "meas_mic",   # FOH measurement mic — the "truth about the room"
}
ROLE_LABELS = {
    "lead_vox": "Lead vocal",
    "harm_vox_l": "Harmony vox L",
    "harm_vox_r": "Harmony vox R",
    "acoustic_gtr": "Acoustic gtr",
    "bass_di": "Bass / 2nd gtr",
    "cajon": "Cajón / perc",
    "keys_aux": "Keys / aux",
    "meas_mic": "FOH meas mic",
}

LEAD_VOCAL_CH = 1
MEAS_MIC_CH = 8               # excluded from the main mix; analysis only
MAIN_BUS = "main/st"          # stereo main bus for room-correction PEQ
BALANCE_CHANNELS = (5, 7)     # bass + keys: held toward a stored balance

# ---- Optional extensions (default empty -> no change for the trio) --------
# Larger bands: extend CHANNELS/ROLE_LABELS above, then populate these.
GUEST_CHANNELS = ()           # channels unmuted only on guest songs (template guest=true)
STEREO_LINKS = ()             # channel pairs that move together, e.g. ((2, 3),) for harm L/R
STAGE_MIC_CH = None           # optional 2nd ambient mic for stage-volume sensing (e.g. 9)
STAGE_RISE_DB = 4.0           # stage-level rise (dB) over baseline that prompts an advisory
PHASE_PAIRS = ()              # channel pairs carrying the SAME source (mic+DI, two mics
                              # on one instrument) to check for polarity/comb, e.g. ((4,5),)

# ---- FX buses (performer FX view: per-channel sends + a wet/return) --------
# Maps an FX mixbus index -> name. The console-specific send/return OSC addresses
# in osc.py are tagged VERIFY: confirm them against your firmware.
FX_BUSES = {1: "Reverb", 2: "Delay"}
CHANNEL_EQ_BANDS = 4          # input PEQ bands on the X32/M32 (shared with feedback notches)
EQ_DEFAULT_FREQS = (120.0, 500.0, 2000.0, 8000.0)   # default band centre freqs (flat)


# ---- Template-driven channel map -----------------------------------------
def apply_channel_map(spec: dict) -> None:
    """Replace the active channel map from a show template's `channels` block.
    MUST be called before constructing the Engine (every reader takes C.* at
    construction/runtime, so mutating these globals in place propagates). Lets a
    13-input AutoFOH rig and the 8-ch trio be the same code, config-only."""
    global LEAD_VOCAL_CH, MEAS_MIC_CH, BALANCE_CHANNELS
    global GUEST_CHANNELS, STEREO_LINKS, STAGE_MIC_CH, PHASE_PAIRS
    # Build everything into locals first so a malformed spec raises BEFORE we touch
    # any global -> the active map is never left half-applied.
    new_chans = ({int(k): str(v) for k, v in spec["map"].items()}
                 if spec.get("map") else dict(CHANNELS))
    labels = spec.get("labels") or {}
    new_labels = {r: labels.get(r, ROLE_LABELS.get(r, r.replace("_", " ").title()))
                  for r in new_chans.values()}
    new_lead = int(spec["lead"]) if spec.get("lead") is not None else LEAD_VOCAL_CH
    new_meas = int(spec["meas_mic"]) if spec.get("meas_mic") is not None else MEAS_MIC_CH
    new_bal = (tuple(int(x) for x in (spec["balance"] or ()))
               if "balance" in spec else BALANCE_CHANNELS)
    new_guest = (tuple(int(x) for x in (spec["guest"] or ()))
                 if "guest" in spec else GUEST_CHANNELS)
    new_links = (tuple(tuple(int(x) for x in pair) for pair in (spec["stereo_links"] or ()))
                 if "stereo_links" in spec else STEREO_LINKS)
    new_phase = (tuple(tuple(int(x) for x in pair) for pair in (spec["phase_pairs"] or ()))
                 if "phase_pairs" in spec else PHASE_PAIRS)
    if "stage_mic" in spec:
        sm = spec["stage_mic"]
        new_stage = int(sm) if sm is not None else None
    else:
        new_stage = STAGE_MIC_CH
    # commit atomically
    CHANNELS.clear(); CHANNELS.update(new_chans)
    ROLE_LABELS.clear(); ROLE_LABELS.update(new_labels)
    LEAD_VOCAL_CH, MEAS_MIC_CH, BALANCE_CHANNELS = new_lead, new_meas, new_bal
    GUEST_CHANNELS, STEREO_LINKS, STAGE_MIC_CH = new_guest, new_links, new_stage
    PHASE_PAIRS = new_phase


def auto_channel_map(n_inputs: int) -> dict:
    """Build + apply a channel map sized to a detected device: the last input is
    the measurement mic, the rest are generic inputs. 1 input -> meas-mic only."""
    n = max(1, int(n_inputs))
    if n == 1:
        spec = {"map": {"1": "meas_mic"}, "meas_mic": 1}
    else:
        m = {str(i): f"in_{i}" for i in range(1, n)}
        m[str(n)] = "meas_mic"
        spec = {"map": m, "meas_mic": n}
    spec.update({"balance": [], "guest": [], "stereo_links": [], "stage_mic": None,
                 "phase_pairs": []})
    apply_channel_map(spec)
    return spec


def channel_map_state() -> tuple:
    """Snapshot the channel-map globals (for test isolation / restore)."""
    return (dict(CHANNELS), dict(ROLE_LABELS), LEAD_VOCAL_CH, MEAS_MIC_CH,
            BALANCE_CHANNELS, GUEST_CHANNELS, STEREO_LINKS, STAGE_MIC_CH, PHASE_PAIRS)


def restore_channel_map_state(s: tuple) -> None:
    global LEAD_VOCAL_CH, MEAS_MIC_CH, BALANCE_CHANNELS
    global GUEST_CHANNELS, STEREO_LINKS, STAGE_MIC_CH, PHASE_PAIRS
    CHANNELS.clear(); CHANNELS.update(s[0])
    ROLE_LABELS.clear(); ROLE_LABELS.update(s[1])
    (LEAD_VOCAL_CH, MEAS_MIC_CH, BALANCE_CHANNELS,
     GUEST_CHANNELS, STEREO_LINKS, STAGE_MIC_CH, PHASE_PAIRS) = s[2:]

# ---- Lead-vocal level ride ------------------------------------------------
# Target *output* loudness for the lead vocal (dB). The ride holds output
# (input tap + fader) at this value as the input level drifts, so when the
# singer backs off the fader eases up to compensate. Tune at soundcheck.
LEAD_TARGET_DB = -6.0
LEAD_TOLERANCE = 2.0          # don't chase moves smaller than this (dB)

# ---- Guardrails — the assistant may never move outside these --------------
FADER_MIN_DB = -12.0
FADER_MAX_DB = +6.0
MAX_STEP_DB = 1.0             # largest single correction
RAMP_MS = 350                # fader move smoothing
HEADAMP_MIN_DB = -12.0
HEADAMP_MAX_DB = +60.0

# ---- Feedback detector ----------------------------------------------------
FB_RING_DB = 12.0            # peak-above-neighbours that flags a ring
FB_SUSTAIN_BLOCKS = 4        # consecutive blocks before we notch (~85 ms)
FB_NOTCH_GAIN_DB = -9.0
FB_NOTCH_Q = 8.0
FB_STABLE_TOL = 0.03         # freq must stay within 3% to count as "stable"
FB_RISE_DB = 0.3            # rms must climb this much/block to count "rising"
FB_DETECT_FFT = 4096         # rolling high-res FFT for meas-mic ring detection
                             # (4x the block -> ~11.7 Hz/bin; no added latency)
FB_HARMONIC_SUSTAIN = 3      # extra sustain blocks demanded from a fully harmonic
                             # (musical) tone, so a held note isn't notched as feedback

# ---- Clip / overload ------------------------------------------------------
CLIP_PEAK_DBFS = -1.0        # within 1 dB of full scale = clipping risk
CLIP_TRIM_DB = -2.0          # preamp back-off per clip event
CLIP_RECOVER_S = 8.0        # clean for this long -> creep gain back up
CLIP_RECOVER_DB = 0.5       # how much to restore per recovery step

# ---- Pink-noise calibration ----------------------------------------------
CAL_DURATION_S = 10.0
CAL_OCTAVE_BANDS = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
CAL_MAX_CUT_DB = -6.0        # most we'll pull down any room peak
CAL_PEAK_THRESH = 4.0        # dB above smoothed avg to count as a "peak"
CAL_PREDIP_DB = -3.0         # pre-emptive dip on top feedback-prone freqs
CAL_N_PREDIP = 2             # how many feedback freqs to pre-dip
CAL_NOISE_DBFS = -20.0       # playback level for the pink-noise test

# ---- Phase / polarity job (auto-flip + coach) -----------------------------
PHASE_CORR_MIN = 0.45        # |correlation| below this -> unrelated sources, ignore
PHASE_INVERT_CORR = -0.35    # smoothed zero-lag corr below this -> polarity inverted
PHASE_COMB_LAG_MS = 0.35     # arrival-time offset above this (correlated) -> comb filtering
PHASE_MAX_LAG_MS = 3.0       # cross-correlation search window (+/- ms)
PHASE_EMA = 1 / 16           # smoothing on corr/lag (anti-flap)
PHASE_SUSTAIN = 8            # sustained inverted blocks before an auto-flip (~170 ms)
PHASE_ACT_COOLDOWN = 5.0     # min seconds between auto-flips on a pair
PHASE_WARN_INTERVAL = 20.0   # min seconds between repeated phase advisories per pair

# ---- Vocal-unmask ducking (dynamic EQ, sidechained to the lead vocal) ------
UNMASK_CHANNELS = ()         # instruments to duck; empty -> every mix ch except the
                             # lead vocal, meas mic, and stage mic
UNMASK_BAND = 4              # the channel PEQ band the duck drives (reserved from notches)
UNMASK_FREQ = 3000.0         # masking centre freq — where vocal intelligibility lives (Hz)
UNMASK_Q = 1.2               # duck bandwidth (moderately wide)
UNMASK_DEPTH_DB = -4.0       # deepest cut applied while the vocal is present
UNMASK_VOX_GATE_DB = -38.0   # smoothed vocal RMS above this = "vocal present"
UNMASK_ATTACK = 0.30         # per-block smoothing when ducking harder (fast, ~tens of ms)
UNMASK_RELEASE = 0.04        # per-block smoothing when releasing (slow, ~hundreds of ms)
UNMASK_WRITE_EPS = 0.3       # only re-send a band when the duck gain moved this many dB

# ---- Coach (advisory) mode ------------------------------------------------
COACH_TTL_S = 8.0            # a standing manual-move recommendation expires this
                            # long after it was last re-issued (problem cleared)

# ---- Defaults for the jobs (Phase 1: safety net only) ---------------------
DEFAULT_ENABLED = {
    "feedback": True,
    "clip": True,
    "vocal_ride": False,
    "balance": False,
    "phase": False,          # polarity/comb check + auto-flip (needs PHASE_PAIRS)
    "unmask": False,         # duck instruments' mid-highs when the vocal is present
}
