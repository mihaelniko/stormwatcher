"""Drive V4L2Camera against a fake driver that answers the same ioctls a real
UVC webcam does, to exercise negotiation, buffer queueing and frame draining."""
import errno
import mmap
import time

import numpy as np

from stormwatch import v4l2
from stormwatch.v4l2 import Mode, V4L2Camera, choose_mode

FMTS = {
    "YUYV": {(640, 480): [30.0], (1280, 720): [10.0]},
    "MJPG": {(640, 480): [60.0, 30.0], (1280, 720): [30.0]},
}


class FakeDriver:
    def __init__(self, fmts=FMTS):
        self.fmts = fmts
        self.mmaps = []
        self.queued = []
        self.filled = []
        self.seq = 0
        self.fmt = None
        self.ctrls = {v4l2.CID_EXPOSURE_AUTO_PRIORITY: 1, v4l2.CID_EXPOSURE_AUTO: 3,
                      v4l2.CID_EXPOSURE_ABSOLUTE: 150}
        self.streaming = False

    # ioctl entry point --------------------------------------------------
    def ioctl(self, fd, req, a):
        V = v4l2
        if req == V.VIDIOC_QUERYCAP:
            a.capabilities = V.CAP_DEVICE_CAPS | V.CAP_VIDEO_CAPTURE | V.CAP_STREAMING
            a.device_caps = V.CAP_VIDEO_CAPTURE | V.CAP_STREAMING
            a.card[:7] = list(b"FakeCam")
        elif req == V.VIDIOC_ENUM_FMT:
            names = list(self.fmts)
            if a.index >= len(names):
                raise OSError(errno.EINVAL, "end")
            a.pixelformat = V.fourcc(names[a.index])
        elif req == V.VIDIOC_ENUM_FRAMESIZES:
            sizes = list(self.fmts.get(V.fourcc_str(a.pixel_format), {}))
            if a.index >= len(sizes):
                raise OSError(errno.EINVAL, "end")
            a.type = V.FRMSIZE_TYPE_DISCRETE
            a.discrete.width, a.discrete.height = sizes[a.index]
        elif req == V.VIDIOC_ENUM_FRAMEINTERVALS:
            rates = self.fmts[V.fourcc_str(a.pixel_format)].get((a.width, a.height), [])
            if a.index >= len(rates):
                raise OSError(errno.EINVAL, "end")
            a.type = V.FRMIVAL_TYPE_DISCRETE
            a.discrete.numerator, a.discrete.denominator = 1000, int(rates[a.index] * 1000)
        elif req == V.VIDIOC_S_FMT:
            p = a.fmt.pix
            fmt = V.fourcc_str(p.pixelformat)
            p.bytesperline = p.width * 2 if fmt == "YUYV" else 0
            p.sizeimage = p.width * p.height * 2
            self.fmt = (fmt, p.width, p.height, p.bytesperline, p.sizeimage)
        elif req == V.VIDIOC_G_PARM:
            a.parm.capture.capability = V.CAP_TIMEPERFRAME
            a.parm.capture.timeperframe.numerator = 1
            a.parm.capture.timeperframe.denominator = 30
        elif req == V.VIDIOC_S_PARM:
            pass
        elif req == V.VIDIOC_REQBUFS:
            a.count = min(a.count, 4)
        elif req == V.VIDIOC_QUERYBUF:
            a.length = self.fmt[4]
            a.m.offset = a.index * 0x100000
        elif req == V.VIDIOC_QBUF:
            assert a.index not in self.queued
            self.queued.append(a.index)
        elif req == V.VIDIOC_DQBUF:
            if not self.filled:
                raise BlockingIOError(errno.EAGAIN, "no frame")
            idx, seq, ts = self.filled.pop(0)
            a.index, a.sequence, a.bytesused = idx, seq, self.fmt[4]
            a.flags = V.BUF_FLAG_TIMESTAMP_MONOTONIC
            a.timestamp.tv_sec, a.timestamp.tv_usec = int(ts), int((ts % 1) * 1e6)
        elif req == V.VIDIOC_STREAMON:
            self.streaming = True
        elif req == V.VIDIOC_STREAMOFF:
            self.streaming = False
        elif req == V.VIDIOC_QUERYCTRL:
            if a.id not in self.ctrls:
                raise OSError(errno.EINVAL, "no ctrl")
            a.minimum, a.maximum = 1, 5000
        elif req == V.VIDIOC_S_CTRL:
            self.ctrls[a.id] = a.value
        elif req == V.VIDIOC_G_CTRL:
            a.value = self.ctrls[a.id]
        else:
            raise OSError(errno.ENOTTY, hex(req))
        return 0

    def mmap(self, fd, length, offset):
        m = mmap.mmap(-1, length)
        self.mmaps.append(m)
        return m

    def produce(self, y_value):
        """The 'sensor' finishes a frame into the oldest queued buffer."""
        idx = self.queued.pop(0)
        self.seq += 1
        fmt, w, h, stride, size = self.fmt
        buf = np.frombuffer(self.mmaps[idx], np.uint8)
        buf[0::2] = y_value  # Y samples of YUYV
        buf[1::2] = 128      # chroma
        self.filled.append((idx, self.seq, time.monotonic() - 0.004))

    def cam(self):
        return V4L2Camera("/dev/fake", ioctl=self.ioctl, mmap_fn=self.mmap,
                          open_fn=lambda p: 99, poll_fn=lambda ms: bool(self.filled),
                          close_fn=lambda fd: None)


def test_mode_choice_prefers_fps_then_raw():
    drv = FakeDriver()
    cam = drv.cam()
    cam.fd = 99
    modes = cam.modes()
    assert Mode("MJPG", 640, 480, 60.0) in modes
    assert choose_mode(modes) == Mode("MJPG", 640, 480, 60.0)
    raw_only = [m for m in modes if m.fmt == "YUYV"]
    assert choose_mode(raw_only) == Mode("YUYV", 640, 480, 30.0)


def test_stream_reads_newest_frame_and_requeues(monkeypatch):
    drv = FakeDriver()
    cam = drv.cam().open()
    assert cam.card == "FakeCam"
    cam.set_mode(Mode("YUYV", 640, 480, 30.0))
    assert cam.stride == 1280
    cam.start(nbuf=3)
    assert sorted(drv.queued) == [0, 1, 2] and drv.streaming

    drv.produce(10)
    drv.produce(200)  # two frames waiting: the older must be skipped
    f = cam.read(timeout=0.1)
    assert f is not None and f.seq == 2
    assert f.luma.shape == (480, 640) and int(f.luma[100, 100]) == 200
    assert 0 < time.monotonic() - f.t < 1.0
    assert sorted(drv.queued) == [0, 2]  # unused buffer + the skipped one went straight back
    cam.release(f)
    assert sorted(drv.queued) == [0, 1, 2]

    assert cam.read(timeout=0.01) is None  # nothing pending
    cam.close()
    assert not drv.streaming


def test_exposure_keeps_frame_rate_constant():
    drv = FakeDriver()
    cam = drv.cam().open()
    notes = cam.configure_exposure(manual_value=300)
    assert drv.ctrls[v4l2.CID_EXPOSURE_AUTO_PRIORITY] == 0
    assert drv.ctrls[v4l2.CID_EXPOSURE_AUTO] == v4l2.EXPOSURE_MANUAL
    assert drv.ctrls[v4l2.CID_EXPOSURE_ABSOLUTE] == 300
    assert notes


def test_mode_parse_roundtrip():
    m = Mode("YUYV", 640, 480, 30.0)
    assert Mode.parse(str(m)) == m
