"""Ways to release the D3300's shutter, fastest first.

* ArduinoShutter - StormTrigger board on the MC-DC2 remote port. The host
  sends one byte; with the photodiode the board fires on its own in ~50 us.
* LineShutter    - any USB-UART adapter: DTR (shutter) and RTS (focus) drive
  optocouplers on the MC-DC2 cable. One ioctl per edge.
* UsbShutter     - no extra hardware: PTP InitiateCapture over the USB cable
  via libgphoto2 (adds a few ms of USB transactions before the release).
* NullShutter    - test mode, fires nothing.

``fire()`` must be cheap and non-blocking: it runs on the detection thread.
"""
from __future__ import annotations

import os
import threading
import time

from .tty import open_tty, set_line


class Shutter:
    kind = "none"
    label = "Test (no release)"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def set_armed(self, armed: bool) -> None:
        pass

    def fire(self) -> float:
        return time.monotonic()


class NullShutter(Shutter):
    pass


class ArduinoShutter(Shutter):
    kind = "arduino"
    label = "Arduino remote cable"

    def __init__(self, link, *, hold_ms=150, awake=True, photodiode=False,
                 pd_threshold=12, pd_k=6, rearm_ms=600):
        self.link = link
        self.hold_ms = hold_ms
        self.awake = awake
        self.photodiode = photodiode
        self.pd_threshold = pd_threshold
        self.pd_k = pd_k
        self.rearm_ms = rearm_ms

    def open(self) -> None:
        self.link.configure(self.pd_threshold, self.pd_k, self.hold_ms, self.rearm_ms)
        self.link.set_auto(False)
        self.link.set_awake(False)

    def set_armed(self, armed: bool) -> None:
        self.link.set_awake(self.awake and armed)
        self.link.set_auto(self.photodiode and armed)

    def fire(self) -> float:
        return self.link.fire()

    def close(self) -> None:
        if self.link.connected:
            self.link.set_auto(False)
            self.link.set_awake(False)


class LineShutter(Shutter):
    kind = "line"
    label = "USB-serial remote cable (DTR/RTS)"

    def __init__(self, path, *, line="dtr", focus_line="rts", hold_ms=150, awake=True,
                 ioctl=None, opener=None):
        self.path = path
        self.line = line
        self.focus_line = focus_line if focus_line != line else None
        self.hold_ms = hold_ms
        self.awake = awake
        self._ioctl = ioctl
        self._opener = opener or open_tty
        self.fd = -1
        self._armed = False
        self._lock = threading.Lock()
        self._release_at = 0.0
        self._timer = None

    def _set(self, line, on):
        if line:
            set_line(self.fd, line, on, ioctl=self._ioctl)

    def open(self) -> None:
        self.fd = self._opener(self.path)
        # The kernel raises DTR and RTS on open; drop them immediately.
        self._set(self.line, False)
        self._set(self.focus_line, False)

    def set_armed(self, armed: bool) -> None:
        with self._lock:
            self._armed = armed
            if self._timer is None:  # not mid-press
                self._set(self.focus_line, self.awake and armed)

    def fire(self) -> float:
        with self._lock:
            self._set(self.focus_line, True)
            self._set(self.line, True)
            t = time.monotonic()
            self._release_at = t + self.hold_ms / 1000.0
            if self._timer is None:
                self._timer = threading.Thread(target=self._releaser, name="line-release", daemon=True)
                self._timer.start()
        return t

    def _releaser(self) -> None:
        while True:
            with self._lock:
                left = self._release_at - time.monotonic()
                if left <= 0:
                    self._set(self.line, False)
                    self._set(self.focus_line, self.awake and self._armed)
                    self._timer = None
                    return
            time.sleep(min(left, 0.05))

    def close(self) -> None:
        if self.fd < 0:
            return
        with self._lock:
            self._release_at = 0.0
        if self._timer:
            self._timer.join(timeout=1.0)
        try:
            self._set(self.line, False)
            self._set(self.focus_line, False)
        finally:
            os.close(self.fd)
            self.fd = -1


class UsbShutter(Shutter):
    kind = "usb"
    label = "USB cable (PTP)"

    def __init__(self, camera):
        self.camera = camera

    def fire(self) -> float:
        return self.camera.request_trigger()
