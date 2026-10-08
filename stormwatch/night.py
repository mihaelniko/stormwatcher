"""Night mode: back-to-back exposures, keep the ones lightning landed in.

At night the reliable way to photograph lightning is to keep the shutter open
continuously; the strike is recorded by whichever exposure it falls into.
Here the camera fires on a fixed rhythm while the detector (webcam or
photodiode) timestamps every flash. Each exposure has a known time window
[release, release + lag + exposure], so a downloaded frame is judged by
whether a flash happened inside ITS window - never by comparing it with the
frame before (which would also keep the dark frame after a strike and could
drop two strikes in a row).

Files are sorted into ``lightning/`` and ``no-lightning/``; nothing is deleted.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque

LAG_MAX = 0.35   # release command -> shutter fully open, generous for the D3300
MARGIN = 0.05


class NightController:
    def __init__(self, shutter, exposure_s: float, gap_s: float, out_dir: str, emit,
                 clock=time.monotonic):
        if not exposure_s or exposure_s <= 0:
            raise ValueError("night mode needs a timed exposure (not Bulb)")
        self.shutter = shutter
        self.exposure = exposure_s
        self.interval = exposure_s + LAG_MAX + max(0.1, gap_s)
        self.out_dir = out_dir
        self.emit = emit
        self.clock = clock
        self.exposures: deque = deque(maxlen=500)
        self.flashes: deque = deque(maxlen=2000)
        self._by_stem: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._next_id = 0
        self._last_id = 0
        self._delay = 0.6       # typical close -> file-on-card delay, learned
        self.kept = 0
        self.rejected = 0

    # -- shutter rhythm --------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="night", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        nxt = self.clock()
        while not self._stop.is_set():
            t = self.shutter.fire()
            self.record_exposure(t)
            nxt = max(nxt + self.interval, self.clock())
            self._stop.wait(max(0.0, nxt - self.clock()))

    def record_exposure(self, t_fire: float) -> dict:
        with self._lock:
            self._next_id += 1
            e = {"id": self._next_id, "t": t_fire, "open": t_fire,
                 "close": t_fire + LAG_MAX + self.exposure, "stem": None, "flash": False}
            for f in self.flashes:
                if e["open"] - MARGIN <= f <= e["close"] + MARGIN:
                    e["flash"] = True
            self.exposures.append(e)
        self.emit({"type": "night_exposure", "id": e["id"]})
        return e

    def record_flash(self, t: float) -> list[int]:
        hit = []
        with self._lock:
            self.flashes.append(t)
            for e in self.exposures:
                if e["open"] - MARGIN <= t <= e["close"] + MARGIN:
                    e["flash"] = True
                    hit.append(e["id"])
        self.emit({"type": "night_flash", "exposures": hit})
        return hit

    # -- files -----------------------------------------------------------------
    def _assign(self, stem: str, t_arrival: float):
        """Match a new file to its exposure by timing.

        A file shows up some write-delay after its exposure closed. Among the
        exposures after the last matched one that have closed, take the one
        whose delay best fits the delay seen so far. An exposure the camera
        never recorded is skipped instead of shifting every later frame.
        """
        if stem in self._by_stem:            # second file of a RAW+JPEG pair
            return self._by_stem[stem]
        cands = [e for e in self.exposures
                 if e["stem"] is None and e["id"] > self._last_id and e["close"] <= t_arrival + 0.25]
        if not cands:
            return None
        best = min(cands, key=lambda e: abs((t_arrival - e["close"]) - self._delay))
        best["stem"] = stem
        self._by_stem[stem] = best
        self._last_id = best["id"]
        self._delay = min(5.0, max(0.0, 0.8 * self._delay + 0.2 * (t_arrival - best["close"])))
        return best

    def on_file(self, path: str | None, stem: str, t_arrival: float) -> None:
        with self._lock:
            e = self._assign(stem, t_arrival)
            flash = bool(e and e["flash"])
        if path is None:  # not downloaded (RAW with JPEG-only policy)
            return
        sub = "lightning" if flash else "no-lightning"
        dest_dir = os.path.join(self.out_dir, sub)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(path))
        try:
            shutil.move(path, dest)
        except OSError as err:
            self.emit({"type": "log", "level": "warn", "msg": f"night: could not sort {path}: {err}"})
            dest = path
        if flash:
            self.kept += 1
        else:
            self.rejected += 1
        self.emit({"type": "night_file", "path": dest, "lightning": flash,
                   "exposure": e["id"] if e else None, "kept": self.kept, "rejected": self.rejected})
