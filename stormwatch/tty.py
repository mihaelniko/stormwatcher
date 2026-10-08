"""Raw serial ports via termios: no pyserial, no line buffering, exclusive.

Used for the StormTrigger Arduino and for the bare USB-UART "line" shutter,
where the shutter is a modem-control line (DTR/RTS) toggled with one ioctl.
"""
from __future__ import annotations

import fcntl
import glob
import os
import struct
import termios
from dataclasses import dataclass

_BAUDS = {9600: termios.B9600, 57600: termios.B57600, 115200: termios.B115200,
          230400: termios.B230400}


def open_tty(path: str, baud: int = 115200, *, ioctl=None) -> int:
    """Open ``path`` raw 8N1, non-blocking, exclusive (TIOCEXCL keeps other
    programs such as ModemManager from opening it underneath us)."""
    ioctl = ioctl or fcntl.ioctl
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        try:
            ioctl(fd, termios.TIOCEXCL)
        except OSError:
            pass
        iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
        speed = _BAUDS[baud]
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL  # no HUPCL: no reset on close
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, speed, speed, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)
    except BaseException:
        os.close(fd)
        raise
    return fd


LINES = {"dtr": termios.TIOCM_DTR, "rts": termios.TIOCM_RTS}


def set_line(fd: int, line: str, asserted: bool, *, ioctl=None) -> None:
    ioctl = ioctl or fcntl.ioctl
    req = termios.TIOCMBIS if asserted else termios.TIOCMBIC
    ioctl(fd, req, struct.pack("i", LINES[line]))


@dataclass
class SerialPort:
    path: str
    stable_path: str = ""
    vid: str = ""
    pid: str = ""
    product: str = ""
    manufacturer: str = ""

    @property
    def label(self) -> str:
        name = self.product or self.manufacturer or "serial port"
        ids = f" [{self.vid}:{self.pid}]" if self.vid else ""
        return f"{os.path.basename(self.path)} - {name}{ids}"

    @property
    def likely_arduino(self) -> bool:
        return self.vid in ("2341", "2a03", "1a86", "10c4", "0403") or "arduino" in self.product.lower()


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def list_ports() -> list[SerialPort]:
    by_id = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        by_id[os.path.realpath(link)] = link
    out = []
    for path in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
        name = os.path.basename(path)
        port = SerialPort(path, by_id.get(path, ""))
        d = os.path.realpath(f"/sys/class/tty/{name}/device")
        for _ in range(6):  # walk up to the USB device that has the IDs
            if os.path.exists(os.path.join(d, "idVendor")):
                port.vid = _read(os.path.join(d, "idVendor"))
                port.pid = _read(os.path.join(d, "idProduct"))
                port.product = _read(os.path.join(d, "product"))
                port.manufacturer = _read(os.path.join(d, "manufacturer"))
                break
            d = os.path.dirname(d)
        out.append(port)
    return out
