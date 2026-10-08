import time

import numpy as np
import pytest

from stormwatch.detector import FlashDetector, thresholds

W, H, FPS = 640, 480, 60.0
DT = 1.0 / FPS


def sky(rng, level=30.0, sigma=3.0):
    return np.clip(rng.normal(level, sigma, (H, W)), 0, 255)


def run(det, frames, t0=0.0):
    out = []
    for i, f in enumerate(frames):
        out.append(det.process(np.asarray(f, np.uint8), t0 + i * DT))
    return out


def warm(det, rng, n=60, **kw):
    return run(det, [sky(rng, **kw) for _ in range(n)])


def bolt(frame, x0=300, gain=180.0, width=1, y0=0, y1=H):
    f = frame.copy()
    x = x0
    rng = np.random.default_rng(1)
    for y in range(y0, y1):
        x = int(np.clip(x + rng.integers(-1, 2), 0, W - width))
        f[y, x:x + width] += gain
    return np.clip(f, 0, 255)


def test_quiet_on_night_noise():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    res = warm(det, rng, n=600, sigma=4.0)
    assert not any(r.flash for r in res)


def test_fires_on_thin_bolt_first_frame():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng)
    r = det.process(bolt(sky(rng)).astype(np.uint8), 61 * DT)
    assert r.flash and r.score >= 1.0


def test_fires_on_sheet_lightning():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.5)
    warm(det, rng)
    r = det.process(np.clip(sky(rng) + 35, 0, 255).astype(np.uint8), 61 * DT)
    assert r.flash and r.coverage > 0.5


def test_fires_in_daylight():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng, level=190.0, sigma=1.5)
    r = det.process(bolt(sky(rng, 190.0, 1.5), gain=50.0, width=2).astype(np.uint8), 61 * DT)
    assert r.flash


def test_ignores_gradual_brightening_and_auto_exposure_ramp():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.9)
    warm(det, rng)
    # +2 levels per frame for 3 s: dusk, headlights fading in, AE hunting.
    res = run(det, [sky(rng, 30.0 + 2.0 * i) for i in range(180)], t0=61 * DT)
    assert not any(r.flash for r in res)


def test_ignores_single_twinkling_pixel():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng)
    frames = []
    for i in range(120):
        f = sky(rng)
        if i % 10 < 5:
            f[100:102, 100:102] = 255  # aircraft strobe / star
        frames.append(f)
    assert not any(r.flash for r in run(det, frames, t0=61 * DT))


def test_second_stroke_fires_again():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng)
    base = 61 * DT
    seq = [bolt(sky(rng)), sky(rng), sky(rng), bolt(sky(rng))]
    flashes = [r.flash for r in run(det, seq, t0=base)]
    assert flashes == [True, False, False, True]


def test_long_flash_does_not_retrigger_every_frame():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng)
    lit = [np.clip(sky(rng) + 60, 0, 255) for _ in range(20)]
    flashes = [r.flash for r in run(det, lit, t0=61 * DT)]
    assert flashes[0] and not any(flashes[1:])


def test_nothing_fires_while_warming_up():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=1.0)
    res = run(det, [sky(rng), bolt(sky(rng)), sky(rng)])
    assert not any(r.flash for r in res)


def test_sensitivity_monotonic():
    lo, hi = thresholds(0.0), thresholds(1.0)
    assert lo[0] > hi[0] and lo[1] > hi[1] and lo[2] > hi[2]


@pytest.mark.parametrize("w,h", [(640, 480), (1280, 720)])
def test_fast_enough(w, h):
    rng = np.random.default_rng(0)
    det = FlashDetector()
    frames = [rng.integers(0, 60, (h, w), dtype=np.uint8) for _ in range(4)]
    for i in range(10):
        det.process(frames[i % 4], i * DT)
    n, t0 = 200, time.perf_counter()
    for i in range(n):
        det.process(frames[i % 4], (10 + i) * DT)
    per_frame_ms = (time.perf_counter() - t0) / n * 1000
    print(f"{w}x{h}: {per_frame_ms:.3f} ms/frame")
    assert per_frame_ms < 5.0


def test_flash_detected_after_sustained_brightening():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng)
    # Lights come up over 1 s and stay: no trigger, and no blind spot after.
    frames = [sky(rng, 30.0 + min(80.0, 80.0 * i / 60)) for i in range(120)]
    assert not any(r.flash for r in run(det, frames, t0=61 * DT))
    r = det.process(bolt(sky(rng, 110.0)).astype(np.uint8), (61 + 120) * DT)
    assert r.flash


def test_flash_detected_during_exposure_ramp():
    rng = np.random.default_rng(0)
    det = FlashDetector(sensitivity=0.7)
    warm(det, rng)
    frames = [sky(rng, 30.0 + 1.5 * i) for i in range(40)]
    assert not any(r.flash for r in run(det, frames, t0=61 * DT))
    r = det.process(bolt(sky(rng, 30.0 + 1.5 * 40)).astype(np.uint8), (61 + 40) * DT)
    assert r.flash
