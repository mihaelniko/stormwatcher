"""Host side of the StormTrigger firmware protocol (firmware/stormtrigger).

Host -> board (single bytes so the trigger path is one write syscall):
    t   fire the shutter now            A / a  photodiode auto-fire on / off
    W/w hold FOCUS (keep camera awake)  M / m  telemetry on / off
    ?   status line                     S<thr>,<k>,<hold_ms>,<rearm_ms>\\n  parameters
Board -> host (lines):
    HELLO STORMTRIGGER <proto> <board>
    F <micros> <P|H> <level> <trip>     fired (P = photodiode ISR, H = host 't')
    T <max> <base> <trip>               telemetry every 10 ms (max sample since last)
    S <auto> <awake> <tele> <thr> <k> <hold> <rearm> <base> <dev16>
    OK | E <message>
"""
from __future__ import annotations

import os
import select
import threading
import time

from .tty import open_tty

PROTOCOL = 1


class ArduinoError(OSError):
    pass


class ArduinoLink:
    def __init__(self, path: str, *, on_fire=None, on_telemetry=None, on_log=None):
        self.path = path
        self.on_fire = on_fire or (lambda ev: None)
        self.on_telemetry = on_telemetry or (lambda level, base, trip, t: None)
        self.on_log = on_log or (lambda level, msg: None)
        self.fd = -1
        self.version = None
        self.board = ""
        self.status: dict = {}
        self._hello = threading.Event()
        self._status_evt = threading.Event()
        self._stop = threading.Event()
        self._rx = None
        self._wlock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------
    def open(self, timeout: float = 4.0) -> "ArduinoLink":
        self.fd = open_tty(self.path, 115200)
        self._stop.clear()
        self._rx = threading.Thread(target=self._reader, name="arduino-rx", daemon=True)
        self._rx.start()
        # Opening the port pulses DTR, which reboots an Uno/Nano (~1.6 s
        # bootloader). Ask again in case the board did not reset.
        deadline = time.monotonic() + timeout
        if not self._hello.wait(min(2.2, timeout)):
            self._write(b"?")
            self._hello.wait(max(0.0, deadline - time.monotonic()))
        if not self._hello.is_set():
            self.close()
            raise ArduinoError(f"no StormTrigger firmware answered on {self.path} "
                               "(flash firmware/stormtrigger/stormtrigger.ino)")
        return self

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            self._write(b"amw")  # disarm, release focus, stop telemetry
        except OSError:
            pass
        self._stop.set()
        if self._rx:
            self._rx.join(timeout=1.0)
        os.close(self.fd)
        self.fd = -1

    @property
    def connected(self) -> bool:
        return self.fd >= 0 and self._hello.is_set()

    # -- commands -------------------------------------------------------------
    def _write(self, data: bytes) -> None:
        with self._wlock:
            view = memoryview(data)
            while view:
                try:
                    n = os.write(self.fd, view)
                    view = view[n:]
                except BlockingIOError:
                    select.select([], [self.fd], [], 0.05)

    def fire(self) -> float:
        """Send the trigger byte; returns the monotonic time it left the host."""
        self._write(b"t")
        return time.monotonic()

    def set_auto(self, on: bool) -> None:
        self._write(b"A" if on else b"a")

    def set_awake(self, on: bool) -> None:
        self._write(b"W" if on else b"w")

    def set_telemetry(self, on: bool) -> None:
        self._write(b"M" if on else b"m")

    def configure(self, thr_min: int, k: int, hold_ms: int, rearm_ms: int) -> None:
        thr_min = max(1, min(250, int(thr_min)))
        k = max(1, min(40, int(k)))
        hold_ms = max(20, min(30000, int(hold_ms)))
        rearm_ms = max(50, min(60000, int(rearm_ms)))
        self._write(f"S{thr_min},{k},{hold_ms},{rearm_ms}\n".encode())

    def request_status(self, timeout: float = 1.0) -> dict:
        self._status_evt.clear()
        self._write(b"?")
        self._status_evt.wait(timeout)
        return self.status

    # -- receive --------------------------------------------------------------
    def _reader(self) -> None:
        buf = b""
        poller = select.poll()
        poller.register(self.fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        while not self._stop.is_set():
            try:
                events = poller.poll(100)
            except InterruptedError:
                continue
            if not events:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                self.on_log("error", f"Arduino link lost: {e}")
                break
            if not chunk:
                if any(ev & (select.POLLHUP | select.POLLERR) for _, ev in events):
                    self.on_log("error", "Arduino disconnected")
                    break
                continue
            t = time.monotonic()
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                self._handle(raw.strip().decode("ascii", "replace"), t)
        self._hello.clear()

    def _handle(self, line: str, t: float) -> None:
        if not line:
            return
        parts = line.split()
        try:
            kind = parts[0]
            if kind == "T" and len(parts) >= 4:
                self.on_telemetry(int(parts[1]), int(parts[2]), int(parts[3]), t)
            elif kind == "F" and len(parts) >= 5:
                self.on_fire({"micros": int(parts[1]), "src": parts[2], "level": int(parts[3]),
                              "trip": int(parts[4]), "t": t})
            elif kind == "HELLO" and len(parts) >= 3 and parts[1] == "STORMTRIGGER":
                self.version = int(parts[2])
                self.board = parts[3] if len(parts) > 3 else ""
                if self.version != PROTOCOL:
                    self.on_log("warn", f"firmware protocol {self.version}, app expects {PROTOCOL}")
                self._hello.set()
            elif kind == "S" and len(parts) >= 10:
                keys = ("auto", "awake", "telemetry", "thr_min", "k", "hold_ms", "rearm_ms",
                        "base", "dev16")
                self.status = dict(zip(keys, map(int, parts[1:10])))
                self._hello.set()
                self._status_evt.set()
            elif kind == "E":
                self.on_log("warn", "Arduino: " + line[2:])
        except (ValueError, IndexError):
            self.on_log("warn", f"Arduino: unparsable line {line!r}")
