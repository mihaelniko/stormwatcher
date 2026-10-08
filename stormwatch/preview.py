"""Preview frames from the engine process to the UI through shared memory.

A seqlock: the writer bumps the sequence to odd, writes, bumps to even; the
reader copies and retries if the sequence moved. Neither side ever blocks the
other, so the UI cannot stall the detection thread.
"""
from __future__ import annotations

import struct
from multiprocessing import shared_memory

import numpy as np

MAX_W, MAX_H = 960, 720
_HDR = struct.Struct("<QIIII")  # seq, width, height, channels, flags
_DATA = 64
SIZE = _DATA + MAX_W * MAX_H * 3


class SharedPreview:
    def __init__(self, shm: shared_memory.SharedMemory, owner: bool):
        self.shm = shm
        self.owner = owner
        self.name = shm.name
        self._seq = 0
        self._buf = shm.buf

    @classmethod
    def create(cls) -> "SharedPreview":
        shm = shared_memory.SharedMemory(create=True, size=SIZE)
        shm.buf[:_DATA] = bytes(_DATA)
        return cls(shm, True)

    @classmethod
    def attach(cls, name: str) -> "SharedPreview":
        # The creator owns the segment. Attaching must not register it with
        # the resource tracker (Python < 3.13 does), or it gets unlinked or
        # double-unregistered when this side exits.
        try:
            shm = shared_memory.SharedMemory(name=name, track=False)
        except TypeError:
            from multiprocessing import resource_tracker

            orig = resource_tracker.register
            resource_tracker.register = lambda n, rtype: None if rtype == "shared_memory" else orig(n, rtype)
            try:
                shm = shared_memory.SharedMemory(name=name)
            finally:
                resource_tracker.register = orig
        return cls(shm, False)

    def write(self, img: np.ndarray, flags: int = 0) -> None:
        h, w = img.shape[:2]
        ch = 1 if img.ndim == 2 else img.shape[2]
        if w > MAX_W or h > MAX_H:
            raise ValueError("preview too large")
        n = w * h * ch
        self._seq += 1
        _HDR.pack_into(self._buf, 0, self._seq * 2 - 1, 0, 0, 0, 0)        # odd: writing
        self._buf[_DATA:_DATA + n] = np.ascontiguousarray(img, np.uint8).reshape(-1).data
        _HDR.pack_into(self._buf, 0, self._seq * 2, w, h, ch, flags)        # even: stable

    def read(self, last_seq: int = 0):
        """Return (seq, image, flags); image is None if nothing new."""
        for _ in range(3):
            seq, w, h, ch, flags = _HDR.unpack_from(self._buf, 0)
            if seq == last_seq or seq & 1 or not w:
                return last_seq, None, 0
            n = w * h * ch
            data = bytes(self._buf[_DATA:_DATA + n])
            if _HDR.unpack_from(self._buf, 0)[0] != seq:
                continue  # torn: writer moved on, try again
            img = np.frombuffer(data, np.uint8).reshape((h, w) if ch == 1 else (h, w, ch))
            return seq, img, flags
        return last_seq, None, 0

    def close(self) -> None:
        self._buf = None
        try:
            self.shm.close()
        except (BufferError, OSError):
            pass
        if self.owner:
            try:
                self.shm.unlink()
            except (FileNotFoundError, OSError):
                pass
