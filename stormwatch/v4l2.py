"""Direct Video4Linux2 capture: ioctl + mmap, no OpenCV, no extra buffering.

* The driver fills a small ring of mmap'd buffers; frames are handed out as
  zero-copy views and the buffer is queued back as soon as the caller is done.
* If more than one frame is waiting, older ones are requeued unread so the
  caller always works on the newest frame (no hidden backlog = no latency).
* Frame times come from the kernel's CLOCK_MONOTONIC buffer timestamp, which is
  the same clock as ``time.monotonic()``.
* Raw formats (GREY/YUYV/NV12/...) are preferred because their luminance needs
  no decoding; MJPEG is only used when it buys a higher frame rate.

The struct layouts and ioctl numbers are checked against the C headers by
tests/test_v4l2_abi.py.
"""
from __future__ import annotations

import ctypes as C
import errno
import fcntl
import glob
import mmap
import os
import select
import time
from dataclasses import dataclass, field

import numpy as np

from .pixfmt import FORMAT_RANK, JPEG_FORMATS, SUPPORTED, decode_jpeg_luma, luma_view

# --------------------------------------------------------------------- ABI ---
u8, u32, s32 = C.c_uint8, C.c_uint32, C.c_int32


class v4l2_capability(C.Structure):
    _fields_ = [("driver", u8 * 16), ("card", u8 * 32), ("bus_info", u8 * 32),
                ("version", u32), ("capabilities", u32), ("device_caps", u32),
                ("reserved", u32 * 3)]


class v4l2_fmtdesc(C.Structure):
    _fields_ = [("index", u32), ("type", u32), ("flags", u32), ("description", u8 * 32),
                ("pixelformat", u32), ("mbus_code", u32), ("reserved", u32 * 3)]


class v4l2_frmsize_discrete(C.Structure):
    _fields_ = [("width", u32), ("height", u32)]


class v4l2_frmsize_stepwise(C.Structure):
    _fields_ = [("min_width", u32), ("max_width", u32), ("step_width", u32),
                ("min_height", u32), ("max_height", u32), ("step_height", u32)]


class _frmsize_u(C.Union):
    _fields_ = [("discrete", v4l2_frmsize_discrete), ("stepwise", v4l2_frmsize_stepwise)]


class v4l2_frmsizeenum(C.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("index", u32), ("pixel_format", u32), ("type", u32), ("u", _frmsize_u),
                ("reserved", u32 * 2)]


class v4l2_fract(C.Structure):
    _fields_ = [("numerator", u32), ("denominator", u32)]


class v4l2_frmival_stepwise(C.Structure):
    _fields_ = [("min", v4l2_fract), ("max", v4l2_fract), ("step", v4l2_fract)]


class _frmival_u(C.Union):
    _fields_ = [("discrete", v4l2_fract), ("stepwise", v4l2_frmival_stepwise)]


class v4l2_frmivalenum(C.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("index", u32), ("pixel_format", u32), ("width", u32), ("height", u32),
                ("type", u32), ("u", _frmival_u), ("reserved", u32 * 2)]


class v4l2_pix_format(C.Structure):
    _fields_ = [("width", u32), ("height", u32), ("pixelformat", u32), ("field", u32),
                ("bytesperline", u32), ("sizeimage", u32), ("colorspace", u32),
                ("priv", u32), ("flags", u32), ("ycbcr_enc", u32), ("quantization", u32),
                ("xfer_func", u32)]


class _fmt_u(C.Union):
    # v4l2_window inside the kernel union holds pointers, so the union is
    # pointer-aligned; the c_void_p member reproduces that.
    _fields_ = [("pix", v4l2_pix_format), ("raw_data", u8 * 200), ("_align", C.c_void_p)]


class v4l2_format(C.Structure):
    _fields_ = [("type", u32), ("fmt", _fmt_u)]


class v4l2_requestbuffers(C.Structure):
    _fields_ = [("count", u32), ("type", u32), ("memory", u32), ("capabilities", u32),
                ("flags", u8), ("reserved", u8 * 3)]


class timeval(C.Structure):
    _fields_ = [("tv_sec", C.c_long), ("tv_usec", C.c_long)]


class v4l2_timecode(C.Structure):
    _fields_ = [("type", u32), ("flags", u32), ("frames", u8), ("seconds", u8),
                ("minutes", u8), ("hours", u8), ("userbits", u8 * 4)]


class _buf_m(C.Union):
    _fields_ = [("offset", u32), ("userptr", C.c_ulong), ("planes", C.c_void_p), ("fd", s32)]


class v4l2_buffer(C.Structure):
    _fields_ = [("index", u32), ("type", u32), ("bytesused", u32), ("flags", u32),
                ("field", u32), ("timestamp", timeval), ("timecode", v4l2_timecode),
                ("sequence", u32), ("memory", u32), ("m", _buf_m), ("length", u32),
                ("reserved2", u32), ("request_fd", s32)]


class v4l2_captureparm(C.Structure):
    _fields_ = [("capability", u32), ("capturemode", u32), ("timeperframe", v4l2_fract),
                ("extendedmode", u32), ("readbuffers", u32), ("reserved", u32 * 4)]


class _parm_u(C.Union):
    _fields_ = [("capture", v4l2_captureparm), ("raw_data", u8 * 200)]


class v4l2_streamparm(C.Structure):
    _fields_ = [("type", u32), ("parm", _parm_u)]


class v4l2_control(C.Structure):
    _fields_ = [("id", u32), ("value", s32)]


class v4l2_queryctrl(C.Structure):
    _fields_ = [("id", u32), ("type", u32), ("name", u8 * 32), ("minimum", s32),
                ("maximum", s32), ("step", s32), ("default_value", s32), ("flags", u32),
                ("reserved", u32 * 2)]


def _IOC(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("V") << 8) | nr


def _IOR(nr, t):
    return _IOC(2, nr, C.sizeof(t))


def _IOW(nr, t):
    return _IOC(1, nr, C.sizeof(t))


def _IOWR(nr, t):
    return _IOC(3, nr, C.sizeof(t))


VIDIOC_QUERYCAP = _IOR(0, v4l2_capability)
VIDIOC_ENUM_FMT = _IOWR(2, v4l2_fmtdesc)
VIDIOC_G_FMT = _IOWR(4, v4l2_format)
VIDIOC_S_FMT = _IOWR(5, v4l2_format)
VIDIOC_REQBUFS = _IOWR(8, v4l2_requestbuffers)
VIDIOC_QUERYBUF = _IOWR(9, v4l2_buffer)
VIDIOC_QBUF = _IOWR(15, v4l2_buffer)
VIDIOC_DQBUF = _IOWR(17, v4l2_buffer)
VIDIOC_STREAMON = _IOW(18, C.c_int)
VIDIOC_STREAMOFF = _IOW(19, C.c_int)
VIDIOC_G_PARM = _IOWR(21, v4l2_streamparm)
VIDIOC_S_PARM = _IOWR(22, v4l2_streamparm)
VIDIOC_G_CTRL = _IOWR(27, v4l2_control)
VIDIOC_S_CTRL = _IOWR(28, v4l2_control)
VIDIOC_QUERYCTRL = _IOWR(36, v4l2_queryctrl)
VIDIOC_ENUM_FRAMESIZES = _IOWR(74, v4l2_frmsizeenum)
VIDIOC_ENUM_FRAMEINTERVALS = _IOWR(75, v4l2_frmivalenum)

BUF_TYPE_VIDEO_CAPTURE = 1
MEMORY_MMAP = 1
FIELD_NONE = 1
CAP_VIDEO_CAPTURE = 0x00000001
CAP_STREAMING = 0x04000000
CAP_DEVICE_CAPS = 0x80000000
CAP_TIMEPERFRAME = 0x1000
FRMSIZE_TYPE_DISCRETE = 1
FRMIVAL_TYPE_DISCRETE = 1
BUF_FLAG_ERROR = 0x40
BUF_FLAG_TIMESTAMP_MASK = 0xE000
BUF_FLAG_TIMESTAMP_MONOTONIC = 0x2000
CTRL_FLAG_DISABLED = 0x1

CID_EXPOSURE_AUTO = 0x009A0901
CID_EXPOSURE_ABSOLUTE = 0x009A0902
CID_EXPOSURE_AUTO_PRIORITY = 0x009A0903
CID_AUTOGAIN = 0x00980912
CID_GAIN = 0x00980913
EXPOSURE_MANUAL = 1
EXPOSURE_APERTURE_PRIORITY = 3


def fourcc(code: str) -> int:
    b = code.encode("ascii")
    return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)


def fourcc_str(v: int) -> str:
    return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))


def _cstr(arr) -> str:
    return bytes(arr).split(b"\0", 1)[0].decode("utf-8", "replace")


# ------------------------------------------------------------------- model ---
@dataclass(frozen=True)
class Mode:
    fmt: str
    width: int
    height: int
    fps: float

    def __str__(self):
        return f"{self.fmt} {self.width}x{self.height}@{self.fps:g}"

    @classmethod
    def parse(cls, s: str) -> "Mode":
        fmt, rest = s.split()
        size, fps = rest.split("@")
        w, h = size.lower().split("x")
        return cls(fmt, int(w), int(h), float(fps))


@dataclass
class Frame:
    t: float                    # capture time, monotonic seconds
    seq: int
    luma: np.ndarray            # (h, w) uint8; may view driver memory
    raw: object = None          # raw buffer (memoryview/bytes) for preview/clips
    fmt: str = "GREY"
    width: int = 0
    height: int = 0
    stride: int = 0
    index: int = -1             # driver buffer to requeue
    meta: dict = field(default_factory=dict)


def choose_mode(modes: list[Mode], target_pixels: int = 640 * 480) -> Mode | None:
    """Highest frame rate first (shortest frame interval = lowest detection
    latency), then a format that needs no decoding, then a size near VGA
    (big frames only cost bandwidth and time; detection works at ~200 px)."""
    usable = [m for m in modes if m.fmt in SUPPORTED and m.width >= 320]
    if not usable:
        usable = [m for m in modes if m.fmt in SUPPORTED]
    if not usable:
        return None

    def key(m: Mode):
        return (-round(m.fps), FORMAT_RANK.get(m.fmt, 9), abs(m.width * m.height - target_pixels))

    return min(usable, key=key)


# ------------------------------------------------------------------ device ---
class V4L2Error(OSError):
    pass


class V4L2Camera:
    """One V4L2 capture device. Not thread-safe: use from one thread."""

    def __init__(self, path: str, *, ioctl=None, mmap_fn=None, open_fn=None, poll_fn=None,
                 close_fn=None):
        self.path = path
        self._ioctl_fn = ioctl or fcntl.ioctl
        self._mmap_fn = mmap_fn or (lambda fd, length, offset: mmap.mmap(
            fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=offset))
        self._open_fn = open_fn or (lambda p: os.open(p, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC))
        self._poll_fn = poll_fn
        self._close_fn = close_fn or os.close
        self.fd = -1
        self.card = ""
        self.bus_info = ""
        self.driver = ""
        self.mode: Mode | None = None
        self.stride = 0
        self.sizeimage = 0
        self._bufs: list = []
        self._views: list = []
        self._streaming = False
        self._poller = None
        self._mono_ts = True

    # -- plumbing -----------------------------------------------------------
    def _ioctl(self, req, arg):
        while True:
            try:
                return self._ioctl_fn(self.fd, req, arg)
            except InterruptedError:
                continue

    def open(self) -> "V4L2Camera":
        self.fd = self._open_fn(self.path)
        cap = v4l2_capability()
        self._ioctl(VIDIOC_QUERYCAP, cap)
        caps = cap.device_caps if cap.capabilities & CAP_DEVICE_CAPS else cap.capabilities
        self.card, self.bus_info, self.driver = _cstr(cap.card), _cstr(cap.bus_info), _cstr(cap.driver)
        if not (caps & CAP_VIDEO_CAPTURE and caps & CAP_STREAMING):
            self.close()
            raise V4L2Error(errno.ENODEV, f"{self.path} is not a streaming capture device")
        return self

    def close(self) -> None:
        self.stop()
        if self.fd >= 0:
            self._close_fn(self.fd)
            self.fd = -1

    def __enter__(self):
        return self.open() if self.fd < 0 else self

    def __exit__(self, *exc):
        self.close()

    # -- discovery ----------------------------------------------------------
    def formats(self) -> list[str]:
        out, i = [], 0
        while True:
            d = v4l2_fmtdesc(index=i, type=BUF_TYPE_VIDEO_CAPTURE)
            try:
                self._ioctl(VIDIOC_ENUM_FMT, d)
            except OSError:
                return out
            out.append(fourcc_str(d.pixelformat))
            i += 1

    def frame_sizes(self, fmt: str) -> list[tuple[int, int]]:
        out, i = [], 0
        while True:
            s = v4l2_frmsizeenum(index=i, pixel_format=fourcc(fmt))
            try:
                self._ioctl(VIDIOC_ENUM_FRAMESIZES, s)
            except OSError:
                return out
            if s.type == FRMSIZE_TYPE_DISCRETE:
                out.append((s.discrete.width, s.discrete.height))
            else:  # stepwise/continuous: offer a few common sizes inside the range
                sw = s.stepwise
                for w, h in ((320, 240), (640, 480), (800, 600), (1280, 720)):
                    if sw.min_width <= w <= sw.max_width and sw.min_height <= h <= sw.max_height:
                        out.append((w, h))
                return out
            i += 1

    def frame_rates(self, fmt: str, w: int, h: int) -> list[float]:
        out, i = [], 0
        while True:
            v = v4l2_frmivalenum(index=i, pixel_format=fourcc(fmt), width=w, height=h)
            try:
                self._ioctl(VIDIOC_ENUM_FRAMEINTERVALS, v)
            except OSError:
                return out
            if v.type == FRMIVAL_TYPE_DISCRETE:
                if v.discrete.numerator:
                    out.append(v.discrete.denominator / v.discrete.numerator)
            else:
                mn = v.stepwise.min  # min interval = max fps
                if mn.numerator:
                    out.append(mn.denominator / mn.numerator)
                return out
            i += 1

    def modes(self) -> list[Mode]:
        out = []
        for fmt in self.formats():
            if fmt not in SUPPORTED:
                continue
            for w, h in self.frame_sizes(fmt):
                for fps in self.frame_rates(fmt, w, h) or [30.0]:
                    out.append(Mode(fmt, w, h, round(fps, 2)))
        return out

    # -- configuration --------------------------------------------------------
    def set_mode(self, mode: Mode) -> Mode:
        f = v4l2_format(type=BUF_TYPE_VIDEO_CAPTURE)
        f.fmt.pix.width, f.fmt.pix.height = mode.width, mode.height
        f.fmt.pix.pixelformat = fourcc(mode.fmt)
        f.fmt.pix.field = FIELD_NONE
        self._ioctl(VIDIOC_S_FMT, f)
        pix = f.fmt.pix
        got_fmt = fourcc_str(pix.pixelformat)
        if got_fmt not in SUPPORTED:
            raise V4L2Error(errno.EINVAL, f"driver chose unsupported format {got_fmt}")
        self.stride, self.sizeimage = pix.bytesperline, pix.sizeimage
        fps = mode.fps
        p = v4l2_streamparm(type=BUF_TYPE_VIDEO_CAPTURE)
        try:
            self._ioctl(VIDIOC_G_PARM, p)
            if p.parm.capture.capability & CAP_TIMEPERFRAME:
                p.parm.capture.timeperframe.numerator = 1000
                p.parm.capture.timeperframe.denominator = int(round(mode.fps * 1000))
                self._ioctl(VIDIOC_S_PARM, p)
            tpf = p.parm.capture.timeperframe
            if tpf.numerator:
                fps = tpf.denominator / tpf.numerator
        except OSError:
            pass
        self.mode = Mode(got_fmt, pix.width, pix.height, round(fps, 2))
        return self.mode

    def query_control(self, cid: int):
        q = v4l2_queryctrl(id=cid)
        try:
            self._ioctl(VIDIOC_QUERYCTRL, q)
        except OSError:
            return None
        if q.flags & CTRL_FLAG_DISABLED:
            return None
        return {"name": _cstr(q.name), "min": q.minimum, "max": q.maximum,
                "step": q.step, "default": q.default_value}

    def get_control(self, cid: int):
        c = v4l2_control(id=cid)
        try:
            self._ioctl(VIDIOC_G_CTRL, c)
        except OSError:
            return None
        return c.value

    def set_control(self, cid: int, value: int) -> bool:
        try:
            self._ioctl(VIDIOC_S_CTRL, v4l2_control(id=cid, value=int(value)))
            return True
        except OSError:
            return False

    def configure_exposure(self, manual_value: int | None) -> list[str]:
        """Keep the frame rate constant (auto exposure must not stretch frames
        in the dark) and optionally lock a manual exposure."""
        notes = []
        if self.query_control(CID_EXPOSURE_AUTO_PRIORITY) is not None:
            if self.set_control(CID_EXPOSURE_AUTO_PRIORITY, 0):
                notes.append("constant frame rate (exposure_auto_priority=0)")
        if manual_value is not None and self.query_control(CID_EXPOSURE_AUTO) is not None:
            if self.set_control(CID_EXPOSURE_AUTO, EXPOSURE_MANUAL):
                q = self.query_control(CID_EXPOSURE_ABSOLUTE)
                if q:
                    v = max(q["min"], min(q["max"], int(manual_value)))
                    if self.set_control(CID_EXPOSURE_ABSOLUTE, v):
                        notes.append(f"manual exposure {v}")
        return notes

    # -- streaming ------------------------------------------------------------
    def start(self, nbuf: int = 3) -> None:
        if self.mode is None:
            raise V4L2Error(errno.EINVAL, "set_mode() first")
        rb = v4l2_requestbuffers(count=nbuf, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
        self._ioctl(VIDIOC_REQBUFS, rb)
        if rb.count < 2:
            raise V4L2Error(errno.ENOMEM, "driver gave fewer than 2 buffers")
        self._bufs, self._views = [], []
        for i in range(rb.count):
            b = v4l2_buffer(index=i, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
            self._ioctl(VIDIOC_QUERYBUF, b)
            mm = self._mmap_fn(self.fd, b.length, b.m.offset)
            self._bufs.append(mm)
            self._views.append(memoryview(mm))
        for i in range(rb.count):
            self._qbuf(i)
        self._ioctl(VIDIOC_STREAMON, C.c_int(BUF_TYPE_VIDEO_CAPTURE))
        self._streaming = True
        if self._poll_fn is None:
            self._poller = select.poll()
            self._poller.register(self.fd, select.POLLIN | select.POLLERR)

    def stop(self) -> None:
        if self._streaming:
            try:
                self._ioctl(VIDIOC_STREAMOFF, C.c_int(BUF_TYPE_VIDEO_CAPTURE))
            except OSError:
                pass
            self._streaming = False
        for v in self._views:
            try:
                v.release()
            except BufferError:
                pass
        for mm in self._bufs:
            try:
                mm.close()
            except (BufferError, AttributeError):
                pass  # a caller still holds a view; the GC will unmap it
        self._bufs, self._views = [], []
        if self.fd >= 0:
            try:
                self._ioctl(VIDIOC_REQBUFS, v4l2_requestbuffers(
                    count=0, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP))
            except OSError:
                pass

    def _qbuf(self, index: int) -> None:
        self._ioctl(VIDIOC_QBUF, v4l2_buffer(index=index, type=BUF_TYPE_VIDEO_CAPTURE,
                                             memory=MEMORY_MMAP))

    def _dqbuf(self):
        b = v4l2_buffer(type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
        try:
            self._ioctl(VIDIOC_DQBUF, b)
        except BlockingIOError:
            return None
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return None
            raise
        return b

    def _wait(self, timeout: float) -> bool:
        ms = int(timeout * 1000)
        if self._poll_fn is not None:
            return self._poll_fn(ms)
        return bool(self._poller.poll(ms))

    def read(self, timeout: float = 1.0) -> Frame | None:
        """Block for the next frame (newest if several are queued)."""
        b = self._dqbuf()
        if b is None:
            if not self._wait(timeout):
                return None
            b = self._dqbuf()
            if b is None:
                return None
        while True:  # drain: keep only the newest finished buffer
            nb = self._dqbuf()
            if nb is None:
                break
            self._qbuf(b.index)
            b = nb
        if b.flags & BUF_FLAG_ERROR or b.bytesused == 0:
            self._qbuf(b.index)
            return None
        now = time.monotonic()
        t = now
        if (b.flags & BUF_FLAG_TIMESTAMP_MASK) == BUF_FLAG_TIMESTAMP_MONOTONIC:
            ts = b.timestamp.tv_sec + b.timestamp.tv_usec * 1e-6
            if 0 < now - ts < 5.0:
                t = ts
        m = self.mode
        raw = self._views[b.index][: b.bytesused]
        if m.fmt in JPEG_FORMATS:
            try:
                luma = decode_jpeg_luma(raw, 192)
            except Exception:
                self._qbuf(b.index)
                return None
        else:
            luma = luma_view(raw, m.fmt, m.width, m.height, self.stride)
        return Frame(t=t, seq=b.sequence, luma=luma, raw=raw, fmt=m.fmt, width=m.width,
                     height=m.height, stride=self.stride, index=b.index,
                     meta={"dequeued": now})

    def release(self, frame: Frame) -> None:
        if frame.index >= 0 and self._streaming:
            frame.raw = None
            frame.luma = None
            self._qbuf(frame.index)
            frame.index = -1


# ------------------------------------------------------------- enumeration ---
@dataclass
class VideoDevice:
    path: str
    name: str
    bus: str
    stable_path: str = ""
    internal: bool = False   # built into the laptop (USB port marked "fixed")
    infrared: bool = False   # face-unlock IR camera


def _is_internal(path: str) -> bool:
    """The kernel marks USB ports wired inside the machine as 'fixed'."""
    d = os.path.realpath(f"/sys/class/video4linux/{os.path.basename(path)}/device")
    for _ in range(5):
        f = os.path.join(d, "removable")
        if os.path.exists(f):
            try:
                with open(f) as fh:
                    return fh.read().strip() == "fixed"
            except OSError:
                return False
        d = os.path.dirname(d)
    return False


def list_devices() -> list[VideoDevice]:
    """Capture-capable /dev/video* nodes (metadata nodes are skipped)."""
    by_id = {}
    for link in glob.glob("/dev/v4l/by-id/*") + glob.glob("/dev/v4l/by-path/*"):
        try:
            by_id.setdefault(os.path.realpath(link), link)
        except OSError:
            pass
    out = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: (len(p), p)):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            cap = v4l2_capability()
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
            caps = cap.device_caps if cap.capabilities & CAP_DEVICE_CAPS else cap.capabilities
            if caps & CAP_VIDEO_CAPTURE and caps & CAP_STREAMING:
                name = _cstr(cap.card)
                out.append(VideoDevice(path, name, _cstr(cap.bus_info), by_id.get(path, ""),
                                       internal=_is_internal(path),
                                       infrared=" ir" in f" {name.lower()}" or "infrared" in name.lower()))
        except OSError:
            pass
        finally:
            os.close(fd)
    # External webcams first (the one pointed at the sky), IR face-unlock last.
    out.sort(key=lambda d: (d.infrared, d.internal))
    return out
