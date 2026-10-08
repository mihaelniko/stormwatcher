"""The engine in its own process.

The UI process never touches a device. The engine process owns the camera,
the webcam and the serial ports, runs the detection thread at real-time
priority, and is killed with the UI if the UI dies (PR_SET_PDEATHSIG). The
GIL of the UI (painting, image scaling) therefore cannot delay a release.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import signal


def engine_main(settings: dict, cmd_q, evt_q, shm_name: str | None) -> None:
    os.environ["LANGUAGE"] = "C"       # libgphoto2 labels in English
    os.environ["LC_MESSAGES"] = "C"
    from . import rt

    rt.set_parent_death_signal()
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C belongs to the UI

    from .engine import Engine
    from .preview import SharedPreview
    from .settings import Settings

    preview = SharedPreview.attach(shm_name) if shm_name else None

    def emit(ev):
        evt_q.put(ev)

    eng = Engine(Settings.from_dict(settings), emit, preview=preview)
    try:
        eng.start()
        while True:
            cmd = cmd_q.get()
            if cmd.get("cmd") == "shutdown":
                break
            eng.handle(cmd)
    finally:
        try:
            eng.shutdown()
        finally:
            if preview:
                preview.close()
            evt_q.put({"type": "engine_exit"})


class EngineProcess:
    def __init__(self, settings: dict):
        from .preview import SharedPreview

        ctx = mp.get_context("spawn")
        self.cmd_q = ctx.Queue()
        self.evt_q = ctx.Queue()
        self.preview = SharedPreview.create()
        self.proc = ctx.Process(target=engine_main, name="stormwatch-engine", daemon=True,
                                args=(settings, self.cmd_q, self.evt_q, self.preview.name))

    def start(self) -> None:
        self.proc.start()

    def send(self, cmd: dict) -> None:
        self.cmd_q.put(cmd)

    def events(self, limit: int = 1000) -> list:
        out = []
        for _ in range(limit):
            try:
                out.append(self.evt_q.get_nowait())
            except queue.Empty:
                break
        return out

    @property
    def alive(self) -> bool:
        return self.proc.is_alive()

    def stop(self, timeout: float = 6.0) -> None:
        if self.proc.is_alive():
            self.send({"cmd": "shutdown"})
            self.proc.join(timeout)
            if self.proc.is_alive():
                self.proc.terminate()
                self.proc.join(2)
        self.preview.close()
