"""A synthetic storm: night sky, sensor noise, drifting brightness and
multi-stroke lightning. Used for demo mode (try the app without hardware)
and by the tests."""
from __future__ import annotations

import time

import numpy as np

from .v4l2 import Frame


class SimulatedSource:
    name = "Simulated storm"

    def __init__(self, width=640, height=480, fps=60.0, every=(3.0, 9.0), seed=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.w, self.h, self.fps = width, height, fps
        self.every = every
        self.rng = np.random.default_rng(seed)
        self.clock, self.sleep = clock, sleep
        self.seq = 0
        self.flash_times: list[float] = []   # stroke onsets, for tests
        yy = np.linspace(0, 1, height, dtype=np.float32)[:, None]
        clouds = self.rng.normal(0, 1, (height // 16 + 1, width // 16 + 1)).astype(np.float32)
        clouds = np.kron(clouds, np.ones((16, 16), np.float32))[:height, :width]
        self.base = 18 + 22 * yy + 4 * clouds
        self.noise = [self.rng.normal(0, 3, (height, width)).astype(np.float32) for _ in range(6)]
        self.strokes: list[tuple[float, float, np.ndarray]] = []
        self._t_next_frame = None
        self._t_next_flash = None

    @property
    def mode(self):
        return f"GREY {self.w}x{self.h}@{self.fps:g}"

    def open(self):
        now = self.clock()
        self._t_next_frame = now
        self._t_next_flash = now + self.rng.uniform(*self.every)
        return self

    def close(self):
        pass

    def release(self, frame):
        pass

    def _bolt(self) -> np.ndarray:
        img = np.zeros((self.h, self.w), np.float32)
        x = self.rng.integers(self.w // 5, 4 * self.w // 5)
        for y in range(0, int(self.h * self.rng.uniform(0.6, 1.0))):
            x = int(np.clip(x + self.rng.integers(-2, 3), 1, self.w - 2))
            img[y, x - 1:x + 2] = 200
        glow = np.exp(-((np.arange(self.w) - x) / (self.w / 5)) ** 2)[None, :].astype(np.float32)
        return img + 30 * glow

    def _schedule_flash(self, now: float) -> None:
        shape = self._bolt()
        t = now
        for _ in range(self.rng.integers(1, 5)):        # 1-4 return strokes
            self.strokes.append((t, t + self.rng.uniform(0.02, 0.06), shape * self.rng.uniform(0.5, 1.0)))
            self.flash_times.append(t)
            t += self.rng.uniform(0.04, 0.15)
        self._t_next_flash = now + self.rng.uniform(*self.every)

    def read(self, timeout: float = 1.0):
        wait = self._t_next_frame - self.clock()
        if wait > 0:
            self.sleep(wait)
        t = self.clock()
        self._t_next_frame = max(self._t_next_frame + 1.0 / self.fps, t - 0.5)
        if t >= self._t_next_flash:
            self._schedule_flash(t)
        img = self.base + self.noise[self.seq % len(self.noise)]
        img = img + 3.0 * np.sin(t * 0.2)                # slow drift
        live = []
        for start, end, shape in self.strokes:
            if start <= t <= end + 0.2:
                k = 1.0 if t <= end else np.exp(-(t - end) / 0.05)
                img = img + shape * k
            if t <= end + 0.2:
                live.append((start, end, shape))
        self.strokes = live
        luma = np.clip(img, 0, 255).astype(np.uint8)
        self.seq += 1
        # Stamp the frame when it is ready, like a driver's buffer timestamp.
        return Frame(t=self.clock(), seq=self.seq, luma=luma, raw=None, fmt="GREY",
                     width=self.w, height=self.h, stride=self.w)
