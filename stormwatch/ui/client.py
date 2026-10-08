"""Qt side of the engine process: drains its events into Qt signals and pulls
preview frames out of shared memory, all on the GUI thread via one timer."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from ..process import EngineProcess


def to_qimage(img) -> QImage:
    h, w = img.shape[:2]
    if img.ndim == 2:
        q = QImage(img.data, w, h, img.strides[0], QImage.Format.Format_Grayscale8)
    else:
        q = QImage(img.data, w, h, img.strides[0], QImage.Format.Format_RGB888)
    return q.copy()  # own the pixels; the numpy buffer goes away


class EngineClient(QObject):
    event = Signal(dict)
    preview = Signal(QImage, int)
    died = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: EngineProcess | None = None
        self._seq = 0
        self._stopping = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._pump)

    def start(self, settings: dict) -> None:
        self._stopping = False
        self.proc = EngineProcess(settings)
        self.proc.start()
        self._seq = 0
        self._timer.start()

    def send(self, cmd: dict) -> None:
        if self.proc and self.proc.alive:
            self.proc.send(cmd)

    def stop(self) -> None:
        self._stopping = True
        self._timer.stop()
        if self.proc:
            self.proc.stop()
            self.proc = None

    def _pump(self) -> None:
        p = self.proc
        if p is None:
            return
        for ev in p.events(400):
            self.event.emit(ev)
        seq, img, flags = p.preview.read(self._seq)
        if img is not None:
            self._seq = seq
            self.preview.emit(to_qimage(img), flags)
        if not p.alive and not self._stopping:
            self._timer.stop()
            self.died.emit()
