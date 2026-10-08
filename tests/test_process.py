"""The engine in its own process, as the UI runs it, and the headless CLI."""
import os
import signal
import subprocess
import sys
import time

from stormwatch.process import EngineProcess
from stormwatch.settings import Settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_engine_process_events_preview_and_shutdown(tmp_path):
    s = Settings(detector="simulated", shutter="none", output_dir=str(tmp_path), realtime=False)
    ep = EngineProcess(s.to_dict())
    ep.start()
    events, img, seq = [], None, 0
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end and (img is None or not any(e["type"] == "telemetry" for e in events)):
            events += ep.events()
            seq, frame, _ = ep.preview.read(seq)
            if frame is not None:
                img = frame
            time.sleep(0.05)
        kinds = {e["type"] for e in events}
        assert {"devices", "state", "telemetry"} <= kinds
        assert img is not None and img.shape[:2] == (480, 640)
        ep.send({"cmd": "arm"})
        end = time.monotonic() + 10
        while time.monotonic() < end and not any(e["type"] == "trigger" for e in events):
            events += ep.events()
            time.sleep(0.05)
        assert any(e["type"] == "trigger" for e in events)
    finally:
        ep.stop()
    assert not ep.alive


def test_headless_demo_runs_and_stops_cleanly(tmp_path):
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "cfg"), PYTHONPATH=ROOT)
    p = subprocess.Popen([sys.executable, "-m", "stormwatch", "--headless", "--demo", "--arm",
                          "--set", f"output_dir={tmp_path}", "--set", "realtime=false"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    lines = []

    def reader():
        for line in p.stdout:
            lines.append(line)

    import threading

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    end = time.monotonic() + 25  # the demo storm flashes every 3-9 s
    while time.monotonic() < end and not any("TRIGGER #" in ln for ln in lines):
        time.sleep(0.1)
    p.send_signal(signal.SIGINT)
    p.wait(timeout=15)
    t.join(5)
    out = "".join(lines)
    assert p.returncode == 0, out
    assert "detector: simulated" in out
    assert "TRIGGER #" in out and "stopping" in out
