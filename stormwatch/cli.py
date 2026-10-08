"""Command line: ``stormwatch`` (UI), ``stormwatch --headless`` (terminal),
``stormwatch --list-devices`` and ``stormwatch --demo``."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from . import __version__
from . import settings as settings_mod


def _overrides(s, items):
    d = s.to_dict()
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--set expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        if k not in d:
            raise SystemExit(f"unknown setting {k!r}; known: {', '.join(sorted(d))}")
        if isinstance(d[k], bool):
            v = v.lower() in ("1", "true", "yes", "on")
        d[k] = v
    return settings_mod.Settings.from_dict(d)


def list_devices() -> None:
    from .tty import list_ports
    from .v4l2 import V4L2Camera, choose_mode, list_devices as videos

    print("Webcams (detectors):")
    for d in videos() or []:
        best = ""
        try:
            with V4L2Camera(d.path) as cam:
                best = f"  best mode: {choose_mode(cam.modes())}"
        except OSError:
            pass
        print(f"  {d.path}  {d.name}{best}")
    if not videos():
        print("  (none)")
    print("Serial ports (StormTrigger board / USB-serial cable):")
    for p in list_ports() or []:
        print(f"  {p.label}{'  <- looks like an Arduino' if p.likely_arduino else ''}")
    if not list_ports():
        print("  (none)")
    print("Cameras (libgphoto2):")
    try:
        import gphoto2 as gp

        cl = gp.check_result(gp.gp_camera_autodetect())
        for i in range(cl.count()):
            print(f"  {cl.get_name(i)}  {cl.get_value(i)}")
        if not cl.count():
            print("  (none)")
    except ImportError:
        print("  python3-gphoto2 not installed")


def run_headless(s, arm: bool) -> None:
    from .engine import Engine

    stop = threading.Event()
    last = {"tele": 0.0, "state": None}

    def emit(ev):
        t = ev.get("type")
        if t == "log":
            print(f"[{ev['level']}] {ev['msg']}", flush=True)
        elif t == "trigger":
            r = "in-board" if ev.get("hardware") else (
                f"{ev['reaction_ms']:.2f} ms after the frame" if ev.get("reaction_ms") is not None else "")
            print(f"*** TRIGGER #{ev['id']} via {ev['shutter']} from {ev['source']} {r}", flush=True)
        elif t == "file":
            print(f"photo: {ev['path']}", flush=True)
        elif t == "night_file":
            print(f"night: {'LIGHTNING' if ev['lightning'] else 'no flash'}  {ev['path']}", flush=True)
        elif t == "camera" and "state" in ev and "settings" not in ev:
            print(f"camera: {ev['state']} {ev.get('model', '')} {ev.get('error', '')}", flush=True)
        elif t == "camera" and ev.get("warnings"):
            for w in ev["warnings"]:
                print(f"[camera] {w}", flush=True)
        elif t == "state":
            d, sh = ev["detector"], ev["shutter"]
            lines = (f"detector: {d['kind']} {d['name']} {d['mode']} {d['error'] or ''}",
                     f"shutter:  {sh['kind']} {sh['error'] or ''}   scheduling: {ev['rt']}")
            if lines != last["state"]:
                last["state"] = lines
                print("\n".join(lines), flush=True)
        elif t == "telemetry" and time.monotonic() - last["tele"] > 5:
            last["tele"] = time.monotonic()
            peak = max((p[1] for p in ev["points"]), default=0)
            print(f"  {ev['fps']} fps, frame age {ev['age_ms']} ms, detect {ev['proc_ms']} ms, "
                  f"score {peak:.2f}", flush=True)

    eng = Engine(s, emit)
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    eng.start()
    if arm:
        time.sleep(0.8)  # let the detector learn the sky first
        eng.arm()
    else:
        print("Not armed. Re-run with --arm to fire on lightning.", flush=True)
    while not stop.is_set():
        stop.wait(0.5)
    print("stopping...", flush=True)
    eng.shutdown()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="stormwatch", description="Low-latency lightning trigger for Nikon DSLRs.")
    ap.add_argument("--headless", action="store_true", help="run in this terminal, no window")
    ap.add_argument("--arm", action="store_true", help="arm at start (headless)")
    ap.add_argument("--demo", action="store_true", help="simulated storm, nothing is released")
    ap.add_argument("--list-devices", action="store_true", help="show detectors, cables and cameras")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a setting for this run")
    ap.add_argument("--version", action="version", version=f"stormwatch {__version__}")
    args = ap.parse_args(argv)

    if args.list_devices:
        list_devices()
        return
    s = _overrides(settings_mod.load(), args.set)
    if args.demo:
        s.detector, s.shutter = "simulated", "none"
    if args.headless:
        os.environ.setdefault("LANGUAGE", "C")
        run_headless(s, args.arm)
        return
    try:
        from .ui.app import run
    except ImportError as e:
        sys.exit(f"The window needs PySide6 ({e}). Install python3-pyside6, or use --headless.")
    run(s, demo=args.demo)


if __name__ == "__main__":
    main()
