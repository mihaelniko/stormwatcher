"""Pixel-format helpers.

Detection only needs luminance, so raw formats are read without any colour
conversion: for YUYV/UYVY/NV12/I420/GREY the Y samples are taken straight out
of the driver buffer as a numpy view. JPEG (MJPEG webcams, D3300 live view) is
decoded with libjpeg's DCT scaling straight to greyscale, which skips most of
the decode work.
"""
from __future__ import annotations

import io

import numpy as np

RAW_FORMATS = ("GREY", "YUYV", "YUY2", "UYVY", "NV12", "NV21", "YU12", "YV12")
JPEG_FORMATS = ("MJPG", "JPEG")
SUPPORTED = RAW_FORMATS + JPEG_FORMATS

# Lower rank = preferred. Raw greyscale/YUV needs no decode at all.
FORMAT_RANK = {"GREY": 0, "YUYV": 1, "YUY2": 1, "UYVY": 1, "NV12": 2, "NV21": 2,
               "YU12": 2, "YV12": 2, "MJPG": 5, "JPEG": 5}


def luma_view(buf, fmt: str, width: int, height: int, stride: int = 0) -> np.ndarray:
    """Return the Y plane of a raw frame as a (height, width) uint8 array.

    The result is a view into ``buf`` whenever possible, so it is only valid
    while the underlying buffer is.
    """
    a = np.frombuffer(buf, dtype=np.uint8)
    if fmt in ("YUYV", "YUY2", "UYVY"):
        stride = stride or width * 2
        rows = a[: stride * height].reshape(height, stride)
        off = 0 if fmt != "UYVY" else 1
        return rows[:, off: off + width * 2: 2]
    if fmt in ("GREY", "NV12", "NV21", "YU12", "YV12"):
        stride = stride or width
        return a[: stride * height].reshape(height, stride)[:, :width]
    raise ValueError(f"no raw luma for format {fmt}")


def decode_jpeg_luma(data, target_width: int = 0) -> np.ndarray:
    """Decode a JPEG to greyscale, letting libjpeg downscale by 1/2, 1/4 or 1/8
    when the result would still be at least ``target_width`` wide."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    if target_width:
        w, h = im.size
        scale = 1
        while scale < 8 and w // (scale * 2) >= target_width:
            scale *= 2
        im.draft("L", (w // scale, h // scale))
    else:
        im.draft("L", im.size)
    if im.mode != "L":
        im = im.convert("L")
    return np.asarray(im)


def downscale_mean(luma: np.ndarray, factor: int) -> np.ndarray:
    """Block-average by an integer factor into float32.

    Averaging (rather than skipping pixels) keeps a one-pixel-wide bolt in the
    work image while cutting sensor noise by ``factor``.
    """
    if factor <= 1:
        return luma.astype(np.float32)
    h = luma.shape[0] // factor
    w = luma.shape[1] // factor
    a = luma[: h * factor, : w * factor]
    # Strided slice-adds: ~7x faster than reshape+sum and work on views
    # (e.g. the Y bytes of a YUYV buffer) without a copy.
    rows = a[0::factor].astype(np.uint16)
    for i in range(1, factor):
        rows += a[i::factor]
    s = rows[:, 0::factor].astype(np.uint32)
    for i in range(1, factor):
        s += rows[:, i::factor]
    return s.astype(np.float32) * np.float32(1.0 / (factor * factor))


def _yuv_to_rgb(y, u, v) -> np.ndarray:
    y = y.astype(np.float32) - 16.0
    u = u.astype(np.float32) - 128.0
    v = v.astype(np.float32) - 128.0
    r = 1.164 * y + 1.596 * v
    g = 1.164 * y - 0.392 * u - 0.813 * v
    b = 1.164 * y + 2.017 * u
    rgb = np.stack((r, g, b), axis=-1)
    np.clip(rgb, 0, 255, out=rgb)
    return rgb.astype(np.uint8)


def to_rgb(buf, fmt: str, width: int, height: int, stride: int = 0) -> np.ndarray:
    """Convert a frame to an (h, w, 3) RGB array. Used off the hot path only
    (preview, saved detector clips)."""
    if fmt in JPEG_FORMATS:
        from PIL import Image

        return np.asarray(Image.open(io.BytesIO(bytes(buf))).convert("RGB"))
    a = np.frombuffer(buf, dtype=np.uint8)
    if fmt == "GREY":
        y = luma_view(buf, fmt, width, height, stride)
        return np.repeat(y[:, :, None], 3, axis=2)
    if fmt in ("YUYV", "YUY2", "UYVY"):
        stride = stride or width * 2
        px = a[: stride * height].reshape(height, stride)[:, : width * 2].reshape(height, width // 2, 4)
        if fmt == "UYVY":
            u, y0, v, y1 = px[..., 0], px[..., 1], px[..., 2], px[..., 3]
        else:
            y0, u, y1, v = px[..., 0], px[..., 1], px[..., 2], px[..., 3]
        y = np.empty((height, width), np.uint8)
        y[:, 0::2] = y0
        y[:, 1::2] = y1
        return _yuv_to_rgb(y, np.repeat(u, 2, axis=1), np.repeat(v, 2, axis=1))
    if fmt in ("NV12", "NV21"):
        stride = stride or width
        y = a[: stride * height].reshape(height, stride)[:, :width]
        uv = a[stride * height: stride * height + stride * (height // 2)].reshape(height // 2, stride)[:, :width]
        uv = uv.reshape(height // 2, width // 2, 2)
        u, v = (uv[..., 0], uv[..., 1]) if fmt == "NV12" else (uv[..., 1], uv[..., 0])
        u = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
        v = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
        return _yuv_to_rgb(y, u, v)
    if fmt in ("YU12", "YV12"):
        stride = stride or width
        cs = stride // 2
        y = a[: stride * height].reshape(height, stride)[:, :width]
        o = stride * height
        p1 = a[o: o + cs * (height // 2)].reshape(height // 2, cs)[:, : width // 2]
        o += cs * (height // 2)
        p2 = a[o: o + cs * (height // 2)].reshape(height // 2, cs)[:, : width // 2]
        u, v = (p1, p2) if fmt == "YU12" else (p2, p1)
        u = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
        v = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
        return _yuv_to_rgb(y, u, v)
    raise ValueError(f"cannot convert format {fmt}")
