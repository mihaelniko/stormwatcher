"""Night mode: exposures back to back, keep the ones lightning landed in.

At night the reliable way to photograph lightning is to keep the shutter open;
a strike is recorded by whichever exposure it falls into, first stroke
included. The detector (webcam or photodiode) timestamps every flash, and each
photo is judged by whether a flash happened during ITS OWN exposure.

Exactly one exposure is in flight at a time, so a photo can never be matched
to the wrong exposure:

1. release the shutter;
2. with the USB release, wait until the camera confirms it fired, and take
   that moment as the start of the exposure (a busy camera may fire late);
3. the exposure ends when its photo appears on the camera's card (tethered),
   or after shutter lag + exposure time when there is no USB connection;
4. it held lightning if a flash happened between start and end;
5. its files are sorted into ``lightning/`` or ``no-lightning/`` (nothing is
   deleted) and a line goes into ``night-log.txt``; then the next exposure.

A release the camera never confirms, or a photo that never shows up, is
reported and skipped; it cannot shift the following frames.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque

LAG = 0.25     # release -> shutter open on the D3300, generous: a flash in the
               # extra slack only keeps one extra frame, it never loses one
MARGIN = 0.05  # detector vs camera timing jitter


class NightController:
    def __init__(self, fire, exposure_s: float, gap_s: float, out_dir: str, emit, *,
                 confirm_release: bool = False, file_events: bool = False,
                 photo_timeout: float | None = None, clock=time.monotonic):
        if not exposure_s or exposure_s <= 0:
            raise ValueError("night mode needs a timed exposure (not Bulb)")
        self.fire = fire
        self.exposure = float(exposure_s)
        self.gap = max(0.05, float(gap_s))
        self.out_dir = out_dir
        self.emit = emit
        self.clock = clock
        self.confirm_release = confirm_release
        self.file_events = file_events
        # A long exposure can be followed by an equally long noise-reduction
        # frame before the picture is written.
        self.photo_timeout = photo_timeout or LAG + 2 * self.exposure + 20.0
        self.flashes: deque = deque(maxlen=5000)
        self.exposures: deque = deque(maxlen=2000)
        self.current: dict | None = None
        self._by_stem: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()      # abort now (shutdown)
        self._finishing = False             # no new exposures; finish the open one
        self._finished_at = None
        self._outstanding = 0               # announced files of ours not yet downloaded
        self._released = threading.Event()
        self._got_file = threading.Event()
        self._t_released = 0.0
        self._t_file = 0.0
        self._last_flash_emit = 0.0
        self._next_id = 0
        self._thread = None
        self.total = 0
        self.with_lightning = 0
        self.problems = 0
        self.log_path = os.path.join(out_dir, "night-log.txt")

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="night", daemon=True)
        self._thread.start()

    def finish(self) -> None:
        """Disarm: take no new exposures, but let the open one end, be judged
        and be sorted (lightning in the last exposure must not be lost)."""
        self._finishing = True

    @property
    def alive(self) -> bool:
        """Still needs camera events: exposing, or waiting for downloads."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if self._finished_at is None:
            self._finished_at = self.clock()
        idle = self.clock() - self._finished_at
        # A few seconds of grace for the second file of a RAW+JPEG pair, and
        # up to two minutes for downloads that are still running.
        return idle < 10 or (self._outstanding > 0 and idle < 120)

    def stop(self) -> None:
        """Abort immediately (application shutdown)."""
        self._stop.set()
        self._released.set()
        self._got_file.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set() and not self._finishing:
            try:
                self._one_exposure()
            except Exception as e:  # keep the rhythm going whatever happens
                self.emit({"type": "log", "level": "error", "msg": f"night: {e!r}"})
            if not self._finishing:
                self._stop.wait(self.gap)

    def _wait(self, evt: threading.Event, timeout: float) -> bool:
        return evt.wait(timeout) and not self._stop.is_set()

    # -- one exposure ----------------------------------------------------------
    def _one_exposure(self) -> None:
        with self._lock:
            self._next_id += 1
            e = {"id": self._next_id, "open": None, "close": None, "end": None, "stem": None,
                 "lightning": False, "state": "releasing", "paths": []}
            self.current = e
            self._released.clear()
            self._got_file.clear()
        t = self.fire()
        if self.confirm_release:
            if not self._wait(self._released, self.exposure + 10.0):
                with self._lock:
                    e["state"] = "not released"
                    self.current = None
                if not self._stop.is_set():
                    self.problems += 1
                    self.emit({"type": "log", "level": "warn",
                               "msg": f"night: the camera did not take exposure #{e['id']} (busy, no card?)"})
                    self.emit(self._done_event(e))
                return
            t = self._t_released
        with self._lock:
            e["open"] = t
            e["close"] = t + LAG + self.exposure
            e["state"] = "open"
        self.emit({"type": "night_exposure", "state": "open", "id": e["id"], "t_open": e["open"],
                   "t_close": e["close"], "exposure": self.exposure})

        if self.file_events:
            got = self._wait(self._got_file, self.photo_timeout)
            end = min(e["close"], self._t_file) if got else e["close"]
        else:
            got = True
            self._stop.wait(max(0.0, e["close"] - self.clock()))
            end = e["close"]
        if self._stop.is_set() and not got:
            return

        with self._lock:
            e["end"] = end
            e["lightning"] = any(e["open"] - MARGIN <= f <= end + MARGIN for f in self.flashes)
            e["state"] = "done" if got else "no photo"
            self.current = None
            self.exposures.append(e)
            paths, e["paths"] = e["paths"], []
            self.total += 1
            if e["lightning"]:
                self.with_lightning += 1
            if not got:
                self.problems += 1
        if not got:
            self.emit({"type": "log", "level": "warn",
                       "msg": f"night: no photo arrived for exposure #{e['id']}"})
        self._write_log(e)
        self.emit(self._done_event(e))
        for p in paths:
            self._sort(e, p)

    def _done_event(self, e: dict) -> dict:
        return {"type": "night_exposure", "state": e["state"], "id": e["id"], "lightning": e["lightning"],
                "stem": e["stem"], "t_open": e["open"], "t_end": e["end"], "total": self.total,
                "with_lightning": self.with_lightning, "problems": self.problems}

    def _write_log(self, e: dict) -> None:
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  exposure {e['id']:4d}  "
                        f"{(e['stem'] or '-'):<10}  {'LIGHTNING' if e['lightning'] else 'no flash':<9}"
                        f"  {self.exposure:g} s  {e['state']}\n")
        except OSError:
            pass

    # -- inputs from the detector and the camera --------------------------------
    def record_flash(self, t: float) -> int | None:
        """A flash seen by the detector at monotonic time ``t``."""
        with self._lock:
            self.flashes.append(t)
            e = self.current
            eid = e["id"] if e and e["state"] == "open" and t >= e["open"] - MARGIN else None
        if t - self._last_flash_emit > 0.1:
            self._last_flash_emit = t
            self.emit({"type": "night_flash", "t": t, "exposure": eid})
        return eid

    def on_release(self, t: float) -> None:
        """The camera confirmed the USB release at ``t``."""
        with self._lock:
            if self.current is not None and self.current["state"] == "releasing":
                self._t_released = t
                self._released.set()

    def on_file_added(self, stem: str, t: float) -> None:
        """A new picture appeared on the camera (its second file, e.g. the
        JPEG of a RAW+JPEG pair, belongs to the same exposure)."""
        with self._lock:
            if stem in self._by_stem:
                if self._by_stem[stem] is not None:
                    self._outstanding += 1   # second file of a pair
                return
            e = self.current
            if e is not None and e["stem"] is None and (
                    e["state"] == "open" or (e["state"] == "releasing" and self._released.is_set())):
                e["stem"] = stem
                self._by_stem[stem] = e
                self._outstanding += 1
                self._t_file = t
                self._got_file.set()
            else:
                self._by_stem[stem] = None  # not ours (e.g. released on the body)

    def on_file(self, path: str | None, stem: str) -> None:
        """A picture finished downloading (``path`` None: it stays on the card)."""
        with self._lock:
            e = self._by_stem.get(stem)
            if e is None:
                return
            self._outstanding = max(0, self._outstanding - 1)
            if path is None:
                return
            if e["state"] in ("releasing", "open"):
                e["paths"].append(path)  # sorted when the exposure is judged
                return
        self._sort(e, path)

    def _sort(self, e: dict, path: str) -> None:
        sub = "lightning" if e["lightning"] else "no-lightning"
        dest_dir = os.path.join(self.out_dir, sub)
        dest = os.path.join(dest_dir, os.path.basename(path))
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(path, dest)
        except OSError as err:
            self.emit({"type": "log", "level": "warn", "msg": f"night: could not sort {path}: {err}"})
            dest = path
        self.emit({"type": "night_file", "path": dest, "lightning": e["lightning"], "exposure": e["id"]})
