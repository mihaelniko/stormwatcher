"""Detector clips: the webcam/live-view frames around every trigger.

The DSLR can never show the stroke that triggered it (shutter lag), but the
detector frames can: the trigger frame IS the flash. A small ring of recent
frames is kept; on a trigger the ring plus a few following frames are written
as JPEGs by a background thread, off the detection path.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque

import numpy as np

from .pixfmt import JPEG_FORMATS, to_rgb


class ClipRecorder:
    def __init__(self, out_dir: str, emit, pre: int = 4, post: int = 6):
        self.out_dir = out_dir
        self.emit = emit
        self.pre = pre
        self.post = post
        self._ring: deque = deque(maxlen=pre)
        self._open: list = []          # clips still collecting frames
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._writer = threading.Thread(target=self._write_loop, name="clips", daemon=True)
        self._writer.start()

    @staticmethod
    def _snap(frame) -> tuple:
        raw = frame.raw
        if raw is None:
            data, fmt = np.ascontiguousarray(frame.luma).tobytes(), "GREY"
            w, h, stride = frame.luma.shape[1], frame.luma.shape[0], frame.luma.shape[1]
        else:
            data, fmt = bytes(raw), frame.fmt
            w, h, stride = frame.width, frame.height, frame.stride
        return (frame.t, data, fmt, w, h, stride)

    def trigger(self, trigger_id: int, wall: float) -> None:
        """Called on the detection thread right after the shutter fired; the
        next frame pushed is the trigger frame."""
        self._open.append({"id": trigger_id, "wall": wall, "frames": list(self._ring),
                           "need": self.post + 1, "trigger_index": len(self._ring)})

    def push(self, frame) -> None:
        snap = self._snap(frame)
        self._ring.append(snap)
        if not self._open:
            return
        done = []
        for clip in self._open:
            clip["frames"].append(snap)
            clip["need"] -= 1
            if clip["need"] <= 0:
                done.append(clip)
        for clip in done:
            self._open.remove(clip)
            try:
                self._q.put_nowait(clip)
            except queue.Full:
                self.emit({"type": "log", "level": "warn", "msg": "clip writer busy, clip dropped"})

    def close(self) -> None:
        self._q.put(None)
        self._writer.join(timeout=10)

    def _write_loop(self) -> None:
        from PIL import Image

        while True:
            clip = self._q.get()
            if clip is None:
                return
            stamp = time.strftime("%H%M%S", time.localtime(clip["wall"]))
            d = os.path.join(self.out_dir, "clips", f"{stamp}_trigger{clip['id']:04d}")
            try:
                os.makedirs(d, exist_ok=True)
                paths = []
                for i, (t, data, fmt, w, h, stride) in enumerate(clip["frames"]):
                    rel = i - clip["trigger_index"]
                    name = os.path.join(d, f"{'T' if rel == 0 else ''}{rel:+03d}.jpg")
                    if fmt in JPEG_FORMATS:
                        with open(name, "wb") as f:
                            f.write(data)
                    else:
                        Image.fromarray(to_rgb(data, fmt, w, h, stride)).save(name, quality=90)
                    paths.append(name)
                self.emit({"type": "clip", "trigger_id": clip["id"], "dir": d, "frames": len(paths),
                           "trigger_frame": paths[clip["trigger_index"]] if paths else None})
            except Exception as e:  # disk full, permissions...
                self.emit({"type": "log", "level": "warn", "msg": f"could not save clip: {e}"})
