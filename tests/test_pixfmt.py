import io

import numpy as np
from PIL import Image

from stormwatch.pixfmt import decode_jpeg_luma, downscale_mean, luma_view, to_rgb

W, H = 64, 48


def test_luma_yuyv_and_uyvy_are_views():
    yuyv = np.zeros((H, W * 2), np.uint8)
    yuyv[:, 0::2] = np.arange(W)[None, :]
    yuyv[:, 1::2] = 128
    y = luma_view(yuyv.tobytes(), "YUYV", W, H)
    assert y.shape == (H, W) and (y[5] == np.arange(W)).all()
    uyvy = np.roll(yuyv, 1, axis=1)
    y2 = luma_view(uyvy.tobytes(), "UYVY", W, H)
    assert (y2[5] == np.arange(W)).all()


def test_luma_planar_with_stride():
    stride = W + 16
    plane = np.zeros((H * 3 // 2, stride), np.uint8)
    plane[:H, :W] = 77
    y = luma_view(plane.tobytes(), "NV12", W, H, stride)
    assert y.shape == (H, W) and (y == 77).all()


def test_to_rgb_grey_level_roundtrip():
    yuyv = np.zeros((H, W * 2), np.uint8)
    yuyv[:, 0::2] = 126  # mid grey in studio range
    yuyv[:, 1::2] = 128
    rgb = to_rgb(yuyv.tobytes(), "YUYV", W, H)
    assert rgb.shape == (H, W, 3)
    assert abs(int(rgb[0, 0, 0]) - int(rgb[0, 0, 2])) <= 1  # neutral


def test_jpeg_scaled_greyscale_decode():
    img = Image.new("RGB", (640, 480), (200, 200, 200))
    b = io.BytesIO()
    img.save(b, "JPEG")
    luma = decode_jpeg_luma(b.getvalue(), 160)
    assert luma.ndim == 2 and 160 <= luma.shape[1] < 320
    assert abs(int(luma.mean()) - 200) < 3


def test_downscale_mean_matches_reference():
    rng = np.random.default_rng(0)
    a = rng.integers(0, 256, (48, 66), dtype=np.uint8)
    got = downscale_mean(a, 3)
    ref = a[:48, :66].reshape(16, 3, 22, 3).mean(axis=(1, 3))
    assert np.allclose(got, ref, atol=1e-4)
