"""The ctypes mirror in stormwatch.v4l2 must match the kernel headers exactly:
a wrong size changes the ioctl number, a wrong offset corrupts frames."""
import ctypes
import os
import shutil
import subprocess

import pytest

from stormwatch import v4l2

HERE = os.path.dirname(__file__)


@pytest.fixture(scope="module")
def abi(tmp_path_factory):
    cc = shutil.which("gcc") or shutil.which("cc")
    hdr = "/usr/include/linux/videodev2.h"
    if not cc or not os.path.exists(hdr):
        pytest.skip("needs a C compiler and kernel headers")
    exe = tmp_path_factory.mktemp("abi") / "v4l2_abi"
    subprocess.run([cc, "-Wall", "-o", str(exe), os.path.join(HERE, "abi", "v4l2_abi.c")], check=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        kind, name, value = line.split()
        table[(kind, name)] = int(value)
    return table


def _offset(struct, path):
    off, t = 0, struct
    for part in path.split("."):
        fld = getattr(t, part)
        off += fld.offset
        t = dict((f[0], f[1]) for f in t._fields_)[part]
    return off


def test_struct_sizes(abi):
    for (kind, name), value in abi.items():
        if kind == "size":
            assert ctypes.sizeof(getattr(v4l2, name)) == value, name


def test_field_offsets(abi):
    for (kind, name), value in abi.items():
        if kind == "off":
            sname, fld = name.split(".", 1)
            st = getattr(v4l2, sname)
            if fld == "discrete":
                got = st.u.offset  # anonymous union
            else:
                got = _offset(st, fld)
            assert got == value, name


def test_ioctl_numbers(abi):
    for (kind, name), value in abi.items():
        if kind == "ioctl":
            assert getattr(v4l2, name) == value, name


def test_constants(abi):
    mapping = {
        "V4L2_CID_EXPOSURE_AUTO": v4l2.CID_EXPOSURE_AUTO,
        "V4L2_CID_EXPOSURE_ABSOLUTE": v4l2.CID_EXPOSURE_ABSOLUTE,
        "V4L2_CID_EXPOSURE_AUTO_PRIORITY": v4l2.CID_EXPOSURE_AUTO_PRIORITY,
        "V4L2_CID_AUTOGAIN": v4l2.CID_AUTOGAIN,
        "V4L2_CID_GAIN": v4l2.CID_GAIN,
        "V4L2_EXPOSURE_MANUAL": v4l2.EXPOSURE_MANUAL,
        "V4L2_CAP_VIDEO_CAPTURE": v4l2.CAP_VIDEO_CAPTURE,
        "V4L2_CAP_STREAMING": v4l2.CAP_STREAMING,
        "V4L2_CAP_DEVICE_CAPS": v4l2.CAP_DEVICE_CAPS,
        "V4L2_BUF_FLAG_TIMESTAMP_MASK": v4l2.BUF_FLAG_TIMESTAMP_MASK,
        "V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC": v4l2.BUF_FLAG_TIMESTAMP_MONOTONIC,
        "V4L2_BUF_FLAG_ERROR": v4l2.BUF_FLAG_ERROR,
        "V4L2_CAP_TIMEPERFRAME": v4l2.CAP_TIMEPERFRAME,
        "V4L2_CTRL_FLAG_DISABLED": v4l2.CTRL_FLAG_DISABLED,
    }
    for name, ours in mapping.items():
        assert abi[("const", name)] == ours, name
