"""osc_demo.py — push moves to the console so you can watch X32-Edit / M32-Edit
mirror the app, and validate the OSC scalings against the editor.

It uses the app's REAL encoder (trio_mix.osc.OscConsole), so whatever it sends is
exactly what the assistant would send a real M32C.

  python osc_demo.py                  # one-shot: faders to known dB + mute + EQ notch
  python osc_demo.py --animate        # continuous fader wave (Ctrl+C to stop)
  python osc_demo.py --ip 192.168.1.50    # aim at a REAL desk instead of localhost

Point it at 127.0.0.1 (default) while `run.py --emulate` is running — the emulated
desk relays every move to any connected editor (X32-Edit). Read the dB/Hz/Q the
editor shows back: if it matches what we print, that scaling is verified.
"""
from __future__ import annotations

import argparse
import math
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")          # cp1252 consoles choke on dB/arrows
except (AttributeError, ValueError):
    pass

from trio_mix.osc import OscConsole


def one_shot(con: OscConsole) -> None:
    print("Pushing a known set — read these back off X32-Edit:")
    for ch, db in [(1, 0.0), (2, 5.0), (3, -10.0), (4, -20.0), (5, -40.0)]:
        con.set_fader_db(ch, db)
        print(f"  ch{ch} fader -> {db:+.1f} dB")
    con.set_channel_mute(6, True);  print("  ch6 -> MUTED")
    con.set_channel_mute(7, False); print("  ch7 -> ON")
    con.nudge_gain_db(1, 0.0, 30.0); print("  ch1 preamp -> +30 dB")
    con.set_eq_notch(8, 1, 2500.0, -9.0, 8.0)
    print("  ch8 EQ band1 -> PEQ ~2500 Hz, -9 dB, Q 8")
    con.set_bus_eq(2, 125.0, -5.0, 4.0)
    print("  Main LR EQ band2 -> ~125 Hz, -5 dB, Q 4")


def animate(con: OscConsole, channels: int = 8) -> None:
    print(f"Animating {channels} faders — watch them wave in X32-Edit. Ctrl+C to stop.")
    t0 = time.time()
    try:
        while True:
            t = time.time() - t0
            for ch in range(1, channels + 1):
                # a travelling wave across the channels, within the safe fader range
                db = -8.0 + 11.0 * 0.5 * (1.0 + math.sin(t * 1.6 + ch * 0.7))
                con.set_fader_db(ch, db)
            time.sleep(0.08)
    except KeyboardInterrupt:
        print("\nstopped.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Push OSC moves so X32-Edit mirrors the app.")
    ap.add_argument("--ip", default="127.0.0.1", help="console/emulator IP (default 127.0.0.1)")
    ap.add_argument("--animate", action="store_true", help="continuous fader wave")
    ap.add_argument("--channels", type=int, default=8, help="how many channels to animate")
    args = ap.parse_args()
    con = OscConsole(args.ip)
    print(f"Sending to {args.ip}:10023 via the app's OSC encoder.")
    if args.animate:
        animate(con, args.channels)
    else:
        one_shot(con)
        print("Done. (Use --animate for continuous movement.)")


if __name__ == "__main__":
    main()
