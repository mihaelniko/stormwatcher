"""Lightning flash detector for luminance frames.

Model, per pixel of a block-averaged work image:

* ``bg``     running background (exponential average, time constant ``bg_tau``)
* ``noise``  running mean absolute deviation from the background

With ``margin = max(min_delta, k * noise)`` a pixel is an *onset* when

1. it is lit: above its background by more than ``margin``,
2. it jumped by more than ``margin`` since the previous frame, after removing
   the scene's recent global drift, and
3. at least one of its 8 neighbours is an onset too.

(2) separates lightning (rise time of microseconds, so it lands inside one
frame) from clouds, dusk, headlights and camera auto-exposure, which creep up
over several frames; the drift term (a short average of the frame-to-frame
change in mean level) cancels exposure ramps. (3) rejects scattered
sensor-noise outliers and twinkling points; bolts and lit cloud are spatially
contiguous. Each new stroke of a multi-stroke flash is a fresh jump, so it is
an onset again.

``score = onset_coverage / min_coverage``, so a score of 1.0 is the trigger
level at every sensitivity. Lit pixels adapt 10x slower, so a long
multi-stroke flash cannot teach the background that it is normal.

All per-frame arithmetic runs in preallocated buffers (~0.4 ms for a 640x480
frame on a laptop CPU).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .pixfmt import downscale_mean

WORK_WIDTH = 192  # analysis resolution: enough for thin bolts, ~10-35k pixels


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def thresholds(sensitivity: float) -> tuple[float, float, float]:
    """Map the 0..1 sensitivity knob to (min_coverage, k_sigma, min_delta)."""
    s = min(1.0, max(0.0, float(sensitivity)))
    min_coverage = 10 ** _lerp(math.log10(0.02), math.log10(0.0003), s)  # 2 % .. 0.03 %
    k_sigma = _lerp(9.0, 3.5, s)
    min_delta = _lerp(20.0, 6.0, s)  # luminance levels (0-255)
    return min_coverage, k_sigma, min_delta


@dataclass
class Detection:
    score: float          # >= 1.0 means flash
    flash: bool
    coverage: float       # fraction of work pixels with a coherent sudden onset
    peak: float           # largest rise above background (levels)
    level: float          # mean luminance of the frame (levels)
    warming: bool = False


class FlashDetector:
    def __init__(self, sensitivity: float = 0.7, bg_tau: float = 1.0,
                 noise_tau: float = 4.0, drift_tau: float = 0.1, warmup: float = 0.6):
        self.bg_tau = bg_tau
        self.noise_tau = noise_tau
        self.drift_tau = drift_tau
        self.warmup = warmup
        self.set_sensitivity(sensitivity)
        self.reset()

    def set_sensitivity(self, sensitivity: float) -> None:
        self.sensitivity = min(1.0, max(0.0, float(sensitivity)))
        self.min_coverage, self.k_sigma, self.min_delta = thresholds(self.sensitivity)

    def reset(self) -> None:
        self._shape = None
        self._t_prev = None
        self._t_start = None
        self._factor = None

    @staticmethod
    def work_factor(width: int) -> int:
        return max(1, int(round(width / WORK_WIDTH)))

    def process(self, luma: np.ndarray, t: float) -> Detection:
        """Analyse one luminance frame captured at monotonic time ``t``."""
        if self._factor is None or self._shape is None:
            self._factor = self.work_factor(luma.shape[1])
        return self.process_work(downscale_mean(luma, self._factor), t)

    def _alloc(self, f: np.ndarray) -> None:
        sh = f.shape
        self._shape = sh
        self._bg = f.copy()
        self._noise = np.full(sh, 1.0, np.float32)
        self._prev = f.copy()
        self._rise = np.empty(sh, np.float32)
        self._margin = np.empty(sh, np.float32)
        self._tmp = np.empty(sh, np.float32)
        self._level_prev = float(f.mean())
        self._drift = 0.0
        self._lit = np.zeros(sh, bool)
        self._jump = np.empty(sh, bool)
        self._onset = np.empty(sh, bool)
        self._pad = np.zeros((sh[0] + 2, sh[1] + 2), np.uint8)
        self._nbr = np.empty(sh, np.uint8)

    def process_work(self, f: np.ndarray, t: float) -> Detection:
        if self._shape != f.shape:
            self._alloc(f)
            self._t_prev = self._t_start = t
            return Detection(0.0, False, 0.0, 0.0, float(f.mean()), warming=True)

        dt = min(1.0, max(1.0 / 1000.0, t - self._t_prev))
        a_bg = np.float32(1.0 - math.exp(-dt / self.bg_tau))
        a_n = np.float32(1.0 - math.exp(-dt / self.noise_tau))

        rise, margin, tmp = self._rise, self._margin, self._tmp
        lit, jump, onset = self._lit, self._jump, self._onset
        level = float(f.mean())

        np.subtract(f, self._bg, out=rise)
        np.multiply(self._noise, np.float32(self.k_sigma), out=margin)
        np.maximum(margin, np.float32(self.min_delta), out=margin)
        np.greater(rise, margin, out=lit)
        np.subtract(f, self._prev, out=tmp)
        tmp -= np.float32(self._drift)
        np.greater(tmp, margin, out=jump)
        np.logical_and(lit, jump, out=onset)

        n_on = int(np.count_nonzero(onset))
        coherent = 0
        if n_on:
            # 3x3 neighbourhood count of onset pixels (including self).
            pad, nbr = self._pad, self._nbr
            pad[1:-1, 1:-1] = onset
            h, w = onset.shape
            nbr[...] = 0
            for dy in (0, 1, 2):
                for dx in (0, 1, 2):
                    nbr += pad[dy:dy + h, dx:dx + w]
            coherent = int(np.count_nonzero(onset & (nbr >= 2)))
        coverage = coherent / onset.size
        peak = float(rise.max())

        # Global drift: short average of the per-frame change in mean level,
        # clipped and frozen on flash candidates so a flash cannot cancel the
        # next stroke.
        if coverage < 0.5 * self.min_coverage:
            d = min(self.min_delta, max(-self.min_delta, level - self._level_prev))
            self._drift += (1.0 - math.exp(-dt / self.drift_tau)) * (d - self._drift)
        self._level_prev = level

        # Background: unlit pixels adapt at a_bg, lit pixels at a_bg / 10.
        np.multiply(lit, np.float32(-0.9) * a_bg, out=tmp)
        tmp += a_bg
        tmp *= rise
        self._bg += tmp
        # Noise: mean |rise| over unlit pixels only.
        np.abs(rise, out=tmp)
        tmp -= self._noise
        tmp *= a_n
        tmp *= ~lit
        self._noise += tmp

        self._prev[...] = f
        self._t_prev = t

        if t - self._t_start < self.warmup:
            # Learn the noise floor first; nothing fires while warming up.
            return Detection(0.0, False, coverage, peak, level, warming=True)
        score = coverage / self.min_coverage
        return Detection(score, score >= 1.0, coverage, peak, level)
