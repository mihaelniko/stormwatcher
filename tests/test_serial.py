"""Arduino protocol over a real pseudo-terminal, and the DTR/RTS line shutter."""
import os
import select
import termios
import threading
import time

import pytest

from stormwatch.arduino import ArduinoError, ArduinoLink
from stormwatch.shutter import ArduinoShutter, LineShutter
from stormwatch.tty import open_tty


class FakeBoard(threading.Thread):
    """Plays the StormTrigger firmware on the master side of a pty."""

    def __init__(self, master, hello=True):
        super().__init__(daemon=True)
        self.fd = master
        self.hello = hello
        self.rx = bytearray()
        self.t_fire_rx = None
        self.stop = threading.Event()
        self.tele = False
        self.line = b""

    def send(self, s):
        os.write(self.fd, s.encode() + b"\r\n")

    def run(self):
        if self.hello:
            time.sleep(0.2)  # boot after the host opened (and flushed) the port
            self.send("HELLO STORMTRIGGER 1 328P")
        last_tele = 0
        while not self.stop.is_set():
            r, _, _ = select.select([self.fd], [], [], 0.005)
            if self.tele and time.monotonic() - last_tele > 0.01:
                self.send("T 31 20 44")
                last_tele = time.monotonic()
            if not r:
                continue
            for b in os.read(self.fd, 256):
                c = bytes([b])
                self.rx += c
                if self.line or c == b"S":
                    self.line += c
                    if c == b"\n":
                        self.send("OK")
                        self.line = b""
                    continue
                if c == b"t":
                    self.t_fire_rx = time.monotonic()
                    self.send("F 123456 H 31 44")
                elif c == b"?":
                    self.send("S 1 1 0 12 6 150 600 20 32")
                elif c == b"M":
                    self.tele = True
                elif c == b"m":
                    self.tele = False


@pytest.fixture
def board():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    b = FakeBoard(master)
    b.start()
    yield b, path
    b.stop.set()
    b.join(1)
    os.close(slave)
    os.close(master)


def test_handshake_fire_latency_and_events(board):
    b, path = board
    fires, teles = [], []
    link = ArduinoLink(path, on_fire=fires.append, on_telemetry=lambda *a: teles.append(a))
    link.open(timeout=3)
    assert link.connected and link.version == 1 and link.board == "328P"

    t_sent = link.fire()
    deadline = time.monotonic() + 1
    while b.t_fire_rx is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert b.t_fire_rx is not None
    assert b.t_fire_rx - t_sent < 0.02  # one write() to the wire
    deadline = time.monotonic() + 1
    while not fires and time.monotonic() < deadline:
        time.sleep(0.005)
    assert fires and fires[0]["src"] == "H" and fires[0]["micros"] == 123456

    link.set_telemetry(True)
    time.sleep(0.15)
    assert len(teles) >= 5 and teles[0][:3] == (31, 20, 44)
    st = link.request_status()
    assert st["thr_min"] == 12 and st["hold_ms"] == 150
    link.close()
    assert b"amw" in bytes(b.rx)  # disarmed + released on close


def test_shutter_wraps_link_commands(board):
    b, path = board
    link = ArduinoLink(path).open(timeout=3)
    sh = ArduinoShutter(link, hold_ms=200, awake=True, photodiode=True, pd_threshold=9, pd_k=5)
    sh.open()
    sh.set_armed(True)
    sh.set_armed(False)
    time.sleep(0.1)
    link.close()
    rx = bytes(b.rx)
    assert b"S9,5,200,600\n" in rx
    assert rx.index(b"W") < rx.index(b"w", rx.index(b"W"))
    assert b"A" in rx


def test_no_firmware_raises_quickly():
    master, slave = os.openpty()
    try:
        b = FakeBoard(master, hello=False)
        b.start()
        with pytest.raises(ArduinoError):
            ArduinoLink(os.ttyname(slave)).open(timeout=0.5)
        b.stop.set()
        b.join(1)
    finally:
        os.close(slave)
        os.close(master)


def test_open_tty_is_raw():
    master, slave = os.openpty()
    try:
        fd = open_tty(os.ttyname(slave))
        attrs = termios.tcgetattr(fd)
        assert attrs[3] & termios.ECHO == 0 and attrs[3] & termios.ICANON == 0
        os.close(fd)
    finally:
        os.close(slave)
        os.close(master)


def test_line_shutter_sequence():
    calls = []
    lock_r, lock_w = os.pipe()

    def ioctl(fd, req, arg):
        import struct
        line = {termios.TIOCM_DTR: "dtr", termios.TIOCM_RTS: "rts"}[struct.unpack("i", arg)[0]]
        calls.append((line, req == termios.TIOCMBIS))

    sh = LineShutter("/dev/null", hold_ms=60, awake=True, ioctl=ioctl, opener=lambda p: lock_r)
    sh.open()
    assert calls == [("dtr", False), ("rts", False)]
    calls.clear()
    sh.set_armed(True)
    assert calls == [("rts", True)]
    calls.clear()
    t0 = sh.fire()
    assert calls == [("rts", True), ("dtr", True)]
    time.sleep(0.15)
    assert ("dtr", False) in calls and calls[-1] == ("rts", True)  # focus stays held while armed
    calls.clear()
    sh.set_armed(False)
    assert calls == [("rts", False)]
    sh.close()
    os.close(lock_w)
    assert t0 > 0
