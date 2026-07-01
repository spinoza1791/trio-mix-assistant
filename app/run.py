"""Entry point — launch the mix-assistant dashboard.

    python run.py                 # simulation mode (no hardware needed)
    python run.py --port 8770
    python run.py --hardware --console-ip 192.168.1.50   # real M32C

In simulation mode a closed-loop stage drives the assistant so you can watch
feedback get caught, clips trimmed, and the vocal ride compensate — live.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                  # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from trio_mix.engine import Engine          # noqa: E402
from trio_mix.server import serve            # noqa: E402


def _dev(v):
    """Parse an --audio-device/--output-device value: int index or device name."""
    if v is None:
        return None
    return int(v) if str(v).isdigit() else v


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _all_lan_ips():
    """Every non-loopback IPv4 the machine has — so an operator on a venue with a
    VPN / second NIC can pick the address that's actually on the tablet's network
    (the server binds all interfaces, only the *printed* URL needs the right one)."""
    import socket
    ips = []
    primary = _lan_ip()
    if primary:
        ips.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def _print_qr(url: str) -> None:
    """Print a scannable terminal QR of the URL (optional; needs `qrcode`)."""
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    print("  Scan with the tablet camera:")
    qr.print_ascii(out=sys.stdout, invert=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Acoustic Trio AI Mix-Assistant")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--lan", action="store_true",
                    help="bind to the LAN so tablets can connect "
                         "(shorthand for --host 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--hardware", action="store_true",
                    help="drive a real M32C over OSC (default: simulation)")
    ap.add_argument("--emulate", action="store_true",
                    help="run the FULL hardware stack against an in-process desk "
                         "emulator + timed audio over real sockets (no gear needed)")
    ap.add_argument("--console-ip", default=None)
    ap.add_argument("--audio-device", default=None,
                    help="input audio device (index or name) for the listening half; "
                         "omit to run hardware mode with manual control only")
    ap.add_argument("--output-device", default=None,
                    help="output device (index or name) the calibration pink noise "
                         "plays to — point this at the PA (default: OS default output)")
    ap.add_argument("--input-gain", type=float, default=0.0,
                    help="digital input boost in dB for a quiet mic/interface "
                         "(e.g. a dynamic Samson Q9U); try 20-40")
    ap.add_argument("--channel-map", default=None,
                    help="explicit console-channel:device-column map for the input "
                         "device, e.g. '1:0,2:1,8:9' (console 1-based, column 0-based). "
                         "Default: console N -> device column N-1. Use when the console's "
                         "USB routing doesn't send channel N to USB column N-1.")
    ap.add_argument("--auto", action="store_true",
                    help="auto-detect the best input device, size the channel map "
                         "to it, and auto-set input gain (no --audio-device needed)")
    ap.add_argument("--trace-osc", action="store_true",
                    help="(--emulate) print every OSC address an editor like X32-Edit "
                         "asks the emulated desk for — for protocol fidelity work")
    ap.add_argument("--list-devices", action="store_true",
                    help="list input audio devices and exit")
    ap.add_argument("--https", action="store_true",
                    help="serve over TLS (self-signed) so iPad/Android get "
                         "Wake Lock + a real PWA install")
    ap.add_argument("--template", default=None,
                    help="show-template JSON (songs -> scene + reference levels); "
                         "omit to use the built-in trio setlist")
    ap.add_argument("--ableset-port", type=int, default=None,
                    help="listen for AbleSet song/section OSC on this UDP port "
                         "(hardware mode); enables automatic scene recall")
    ap.add_argument("--no-show-clock", action="store_true",
                    help="disable the simulated show clock (no auto song changes)")
    ap.add_argument("--venue", default="",
                    help="venue name — tags the session log for venue learning")
    ap.add_argument("--session-db", default=None,
                    help="SQLite path for session logging (default: ./sessions.db; "
                         "use 'off' to disable)")
    ap.add_argument("--advisor", action="store_true",
                    help="enable the Claude AI advisory layer (needs ANTHROPIC_API_KEY; "
                         "advisory notes only — never controls the mix)")
    ap.add_argument("--advisor-interval", type=float, default=30.0,
                    help="seconds between advisor checks (default 30)")
    args = ap.parse_args()
    if args.lan:
        args.host = "0.0.0.0"

    if args.list_devices:
        from trio_mix.capture import list_audio_devices
        print(list_audio_devices())
        return

    from trio_mix import template as tmpl
    try:
        show_template = tmpl.load(args.template) if args.template else tmpl.default_template()
    except (OSError, tmpl.TemplateError, ValueError) as exc:
        print(f"  [!] show template error: {exc}")
        sys.exit(2)
    if show_template.channels:          # template-driven map MUST apply before Engine()
        from trio_mix import config as C
        C.apply_channel_map(show_template.channels)
        print(f"  Channel map: {len(C.CHANNELS)} channels from template "
              f"'{show_template.name}'")

    chan_map = None                      # explicit console-ch -> device-column map
    if args.channel_map:
        from trio_mix import config as C
        from trio_mix.capture import parse_channel_map
        try:
            chan_map = parse_channel_map(args.channel_map)
        except ValueError as exc:
            print(f"  [!] --channel-map: {exc}")
            sys.exit(2)
        missing = [ch for ch in C.CHANNELS if ch not in chan_map]
        if missing:                      # unmapped channels would be silently dropped
            print(f"  [!] --channel-map is missing console channel(s) {missing}; "
                  "they won't be captured. Add them or omit --channel-map.")
        if args.auto:                    # --auto builds + sizes its own map
            print("  [i] --channel-map is ignored with --auto (auto sizes its own map).")

    auto_source = None                   # --auto: open ranked devices for real, keep the
                                         # first that actually streams audio (no pre-verify)
    if args.auto:
        from trio_mix import config as C
        from trio_mix.capture import autodetect_inputs, SoundDeviceCapture
        cands = autodetect_inputs()
        if not cands:
            print("  [!] --auto: no input devices found (install sounddevice?) — manual only.")
        for cand in cands[:6]:
            C.auto_channel_map(cand["channels"])      # size the map, then open for real
            s = SoundDeviceCapture(device=cand["index"])
            try:
                s.start()
                s.auto_gain()                          # measures the mic; sets _auto_db
            except Exception as exc:
                print(f"  [i] --auto: [{cand['index']}] {cand['name']} ({cand['hostapi']}) "
                      f"won't open ({type(exc).__name__}); trying next.")
                try: s.stop()
                except Exception: pass
                continue
            if s._auto_db is not None:                 # it actually delivered audio
                auto_source = s
                print(f"  Auto input: [{cand['index']}] {cand['name']} ({cand['hostapi']}, "
                      f"{cand['channels']} ch) — mic {s._auto_db:.0f} dBFS, +{s.gain_db:.0f} dB")
                break
            print(f"  [i] --auto: [{cand['index']}] {cand['name']} ({cand['hostapi']}) opened "
                  f"but delivered no audio (mic busy?); trying next.")
            try: s.stop()
            except Exception: pass
        if auto_source is None and cands:
            print("  [!] --auto: no input delivered audio. Close other apps using the mic")
            print("      (x32 edit, voice tools) and check Windows mic privacy, then restart.")

    emu = None
    if args.emulate:
        # Full hardware stack against an in-process emulator over real UDP sockets.
        from trio_mix.capture import SoundDeviceCapture
        from trio_mix.metersrv import MeterReceiver
        from trio_mix.showclock import SimShowClock
        from trio_mix import config as C
        try:
            from trio_mix.osc import OscConsole
            from trio_mix.emulator import EmulatedDesk, TimedAudioStream
            emu = EmulatedDesk(external_moves=True)     # echoes /xremote + streams /meters
            emu.trace_requests = args.trace_osc         # log what an editor (X32-Edit) asks for
            con = OscConsole("127.0.0.1")
        except RuntimeError as exc:         # python-osc not installed
            print(f"  [!] --emulate needs python-osc: {exc}")
            sys.exit(2)
        if auto_source is not None:
            source = auto_source                       # auto-detected mic (already streaming)
        elif args.audio_device is not None:
            # real capture (e.g. your USB mic) + the emulated desk: lets you test
            # pink-noise calibration and feedback catching with no console.
            source = SoundDeviceCapture(device=_dev(args.audio_device),
                                        output_device=_dev(args.output_device),
                                        channel_map=chan_map, gain_db=args.input_gain)
            print("  Listening on a REAL audio device against the emulated desk."
                  + (f"  (+{args.input_gain:.0f} dB input)" if args.input_gain else ""))
        else:
            source = SoundDeviceCapture()                  # synthetic audio
            source._stream_factory = lambda cb: TimedAudioStream(cb, channels=source.ndev)
        engine = Engine(console=con, sim=False, source=source,
                        tick=C.BLOCK / C.SAMPLE_RATE, template=show_template)
        engine.meter_rx = MeterReceiver(on_fader=engine.reconcile_fader,
                                        on_meters=engine.set_console_meters)
        if not args.no_show_clock:
            engine.show_clock = SimShowClock(show_template, on_change=engine.on_song_change)
    elif args.hardware:
        from trio_mix.capture import SoundDeviceCapture
        from trio_mix.metersrv import MeterReceiver
        from trio_mix import config as C
        try:
            from trio_mix.osc import OscConsole
            con = OscConsole(args.console_ip or C.CONSOLE_IP)
        except RuntimeError as exc:         # python-osc not installed
            print(f"  [!] {exc}")
            print("      Run setup (setup.bat / setup.command) to install dependencies.")
            sys.exit(2)
        source = None
        if auto_source is not None:
            source = auto_source                       # auto-detected mic (already streaming)
        elif args.audio_device is not None:
            source = SoundDeviceCapture(device=_dev(args.audio_device),
                                        output_device=_dev(args.output_device),
                                        channel_map=chan_map, gain_db=args.input_gain)
        # tick the loop near the audio block period so we consume ~1 block/tick
        engine = Engine(console=con, sim=False, source=source,
                        tick=C.BLOCK / C.SAMPLE_RATE, template=show_template)
        engine.meter_rx = MeterReceiver(on_fader=engine.reconcile_fader,
                                        on_meters=engine.set_console_meters)
        # AbleSet -> automatic scene recall (real show clock)
        if args.ableset_port is not None:
            from trio_mix.showclock import AbleSetReceiver
            engine.show_clock = AbleSetReceiver(on_change=engine.on_song_change,
                                                port=args.ableset_port)
    else:
        engine = Engine(sim=True, template=show_template)
        # the simulator walks the setlist so you can watch scenes recall live
        if not args.no_show_clock:
            from trio_mix.showclock import SimShowClock
            engine.show_clock = SimShowClock(show_template,
                                             on_change=engine.on_song_change)

    # session logging (SQLite) — default on, off the real-time path
    db = args.session_db if args.session_db is not None else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    if str(db).lower() != "off":
        from trio_mix.sessionlog import SessionLog
        slog = SessionLog(db)
        slog.start_session(venue=args.venue, template=show_template.name,
                           mode=("emulated" if args.emulate else
                                 "hardware" if args.hardware else "simulation"))
        engine.session_log = slog
        print(f"  Session log: {db}" + (f"  (venue: {args.venue})" if args.venue else ""))

    # Venue learning: load any existing per-venue model, apply it as a prior
    if args.venue:
        from trio_mix import venue as venuemod
        engine.venue = args.venue
        engine.venue_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venues")
        model = venuemod.load_model(args.venue, engine.venue_dir)
        if model is not None:
            engine.apply_venue_model(model)
            print(f"  Venue model: {model.shows} show(s), {len(model.feedback_freqs)} "
                  f"freq(s), confidence {model.confidence:.0%}")
        else:
            print(f"  Venue '{args.venue}': no model yet — will learn after this show.")

    # AI advisory layer (Claude) — opt-in, advisory only
    if args.advisor:
        from trio_mix.advisor import Advisor
        adv = Advisor(get_context=engine.advisor_context, on_advice=engine.on_advice,
                      interval=args.advisor_interval)
        if adv.available:
            engine.advisor = adv
            print("  AI advisor: ON (advisory notes only)")
        else:
            print("  [!] --advisor requested but ANTHROPIC_API_KEY is not set; advisor disabled.")

    ip = _lan_ip()
    if emu is not None:
        try:
            emu.start()             # the desk must be listening before we subscribe
        except OSError as exc:      # emulator OSC port already bound
            print(f"  [!] Desk emulator could not bind OSC port {emu.listen_port}: {exc}")
            print("      Another --emulate session is probably running. Stop it and retry.")
            sys.exit(2)
        # The emulated desk now answers X32-Edit/M32-Edit's discovery handshake, so you
        # can connect the editor to it and watch the app drive a real console GUI.
        from trio_mix import config as C
        print(f"  X32-Edit: connect it to  {ip}:{emu.listen_port}  (or 127.0.0.1 on this PC)")
        print(f"            it mirrors the app live; identity = {C.X32_MODEL}/{C.X32_FW}"
              + ("  [--trace-osc ON]" if args.trace_osc else ""))
    try:
        httpd = serve(engine, args.host, args.port)   # bind first; don't start threads if it fails
    except OSError as exc:
        print(f"  [!] Could not open the dashboard on port {args.port}: {exc}")
        print(f"      Another copy already running? Try a different port:  --port {args.port + 1}")
        if emu is not None:
            emu.stop()
        sys.exit(2)
    engine.start()                       # --auto already started + gained the source above
    scheme = "http"
    if args.https:
        try:
            import ssl
            from trio_mix.tls import ensure_cert
            hosts = ["localhost", "127.0.0.1"] + ([ip] if ip else [])
            certdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".certs")
            certfile, keyfile = ensure_cert(certdir, hosts)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        except Exception as exc:        # missing cryptography+openssl, or cert error
            print(f"  [!] --https unavailable ({type(exc).__name__}: {exc}); "
                  "serving over http.")
            print("      For TLS: pip install cryptography  (or put openssl on PATH).")

    mode = ("EMULATED-HARDWARE" if args.emulate else
            "HARDWARE" if args.hardware else "SIMULATION")
    print(f"Trio Mix-Assistant [{mode}] -> {scheme}://{args.host}:{args.port}/")
    if args.emulate:
        print("  Real OSC stack over localhost sockets against the desk emulator.")
        print("  NB: wire formats are assumptions — see HARDWARE_BRINGUP.md to validate.")
    if args.host in ("0.0.0.0", "::") and ip:
        tablet_url = f"{scheme}://{ip}:{args.port}/"
        print(f"  Tablet (iPad / Galaxy Tab S7): open  {tablet_url}  then Add to Home Screen")
        alts = [a for a in _all_lan_ips() if a != ip]
        if alts:                       # VPN / multiple NICs — the tablet may be on another
            print("  If that's unreachable, try: " +
                  ", ".join(f"{scheme}://{a}:{args.port}/" for a in alts))
        if scheme == "https":
            print("  (self-signed: accept the certificate warning once on the tablet)")
        _print_qr(tablet_url)
    elif scheme == "http":
        print("  For a tablet, restart with  --lan  (add --https for Wake Lock).")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.shutdown()
        engine.stop()
        if emu is not None:
            emu.stop()


if __name__ == "__main__":
    main()
