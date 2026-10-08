"""Custom widgets: live preview, signal graph, status chips, gallery."""
from __future__ import annotations

import os
import time
from collections import deque

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QImageReader, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QWidget

RED = QColor("#e5484d")
AMBER = QColor("#ffb224")
GREEN = QColor("#30a46c")
BLUE = QColor("#3daee9")
GREY = QColor("#8b8d98")
LEVEL_COLORS = {"ok": GREEN, "warn": AMBER, "error": RED, "idle": GREY, "info": BLUE}


class PreviewView(QWidget):
    """The detector's view, with what matters drawn on top."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img: QImage | None = None
        self._armed = False
        self._info = ""
        self._placeholder = "Starting..."
        self._banner = ""
        self._banner_until = 0.0
        self._flash_until = 0.0
        self._t_img = 0.0
        self._tick = QTimer(self)
        self._tick.setSingleShot(True)
        self._tick.timeout.connect(self.update)

    def set_image(self, img: QImage, flags: int = 0) -> None:
        self._img = img
        self._t_img = time.monotonic()
        if flags & 1:
            self._flash_until = time.monotonic() + 0.25
        self.update()

    def clear_image(self, placeholder: str) -> None:
        self._img = None
        self._placeholder = placeholder
        self.update()

    def set_armed(self, armed: bool) -> None:
        self._armed = armed
        self.update()

    def set_info(self, text: str) -> None:
        self._info = text
        self.update()

    def show_trigger(self, text: str) -> None:
        now = time.monotonic()
        self._banner = text
        self._banner_until = now + 2.5
        self._flash_until = now + 0.6
        self.update()
        self._tick.start(2600)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(8, 9, 11))
        r = QRectF(self.rect())
        now = time.monotonic()
        if self._img is not None and not self._img.isNull():
            iw, ih = self._img.width(), self._img.height()
            s = min(r.width() / iw, r.height() / ih)
            tw, th = iw * s, ih * s
            target = QRectF(r.x() + (r.width() - tw) / 2, r.y() + (r.height() - th) / 2, tw, th)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(target, self._img)
            if now - self._t_img > 2.0:
                self._badge(p, QRectF(target.x() + 10, target.bottom() - 34, 170, 24),
                            "no new frames", AMBER)
        else:
            p.setPen(QColor(150, 152, 160))
            f = p.font()
            f.setPointSizeF(f.pointSizeF() * 1.1)
            p.setFont(f)
            p.drawText(r.adjusted(24, 24, -24, -24), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       self._placeholder)

        if self._info:
            p.setFont(QFont(self.font().family(), max(8, int(self.font().pointSizeF() * 0.95))))
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(self._info) + 16
            self._badge(p, QRectF(10, 10, w, fm.height() + 8), self._info, QColor(0, 0, 0, 170), fg=QColor(225, 227, 232))
        if self._armed:
            self._badge(p, QRectF(r.width() - 104, 10, 94, 26), "ARMED", RED, bold=True)

        if now < self._flash_until:
            pen = QPen(AMBER, 6)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(3, 3, -3, -3))
            self._tick.start(int((self._flash_until - now) * 1000) + 20)
        if now < self._banner_until and self._banner:
            f = QFont(self.font())
            f.setPointSizeF(f.pointSizeF() * 1.4)
            f.setBold(True)
            p.setFont(f)
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(self._banner) + 32
            box = QRectF((r.width() - w) / 2, r.height() - fm.height() - 36, w, fm.height() + 14)
            self._badge(p, box, self._banner, AMBER, fg=QColor(20, 20, 20), radius=8)
        p.end()

    def _badge(self, p, rect, label, bg, fg=None, bold=False, radius=5):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, radius, radius)
        p.setPen(fg or QColor(255, 255, 255))
        if bold:
            f = QFont(p.font())
            f.setBold(True)
            p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)


class SignalGraph(QWidget):
    """Detector score over the last few seconds. 1.0 is the trigger line."""

    WINDOW = 12.0
    YMAX = 3.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.points: deque = deque(maxlen=4000)
        self.triggers: deque = deque(maxlen=200)
        self.flashes: deque = deque(maxlen=400)
        self.armed = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(33)

    def add_points(self, pts) -> None:
        self.points.extend(pts)

    def add_trigger(self, t: float) -> None:
        self.triggers.append(t)

    def add_flash(self, t: float) -> None:
        self.flashes.append(t)

    def clear(self) -> None:
        self.points.clear()

    def sizeHint(self):
        return QSize(600, 140)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = self.palette()
        bg = pal.color(QPalette.ColorRole.Base)
        fg = pal.color(QPalette.ColorRole.Text)
        p.fillRect(self.rect(), bg)
        left, top, right, bottom = 36, 8, self.width() - 8, self.height() - 18
        w, h = right - left, bottom - top
        now = time.monotonic()
        t0 = now - self.WINDOW

        def x_of(t):
            return left + (t - t0) / self.WINDOW * w

        def y_of(v):
            return bottom - min(v, self.YMAX) / self.YMAX * h

        grid = QColor(fg)
        grid.setAlpha(40)
        p.setPen(QPen(grid, 1))
        for v in (0.5, 1.5, 2.0, 2.5, 3.0):
            p.drawLine(QPointF(left, y_of(v)), QPointF(right, y_of(v)))
        for s in range(0, int(self.WINDOW) + 1, 2):
            x = x_of(now - s)
            p.drawLine(QPointF(x, top), QPointF(x, bottom))

        label = QColor(fg)
        label.setAlpha(150)
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.8))
        p.setFont(f)
        p.setPen(label)
        for v in (0.0, 1.0, 2.0, 3.0):
            p.drawText(QRectF(0, y_of(v) - 8, left - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{v:g}")
        for s in (0, 4, 8, 12):
            p.drawText(QRectF(x_of(now - s) - 20, bottom + 2, 40, 14), Qt.AlignmentFlag.AlignCenter,
                       "now" if s == 0 else f"-{s}s")

        thr = QPen(RED if self.armed else AMBER, 1.5, Qt.PenStyle.DashLine)
        p.setPen(thr)
        p.drawLine(QPointF(left, y_of(1.0)), QPointF(right, y_of(1.0)))

        for t in self.flashes:
            if t >= t0:
                p.setPen(QPen(QColor(255, 178, 36, 90), 3))
                p.drawLine(QPointF(x_of(t), top), QPointF(x_of(t), bottom))
        for t in self.triggers:
            if t >= t0:
                p.setPen(QPen(RED, 2))
                p.drawLine(QPointF(x_of(t), top), QPointF(x_of(t), bottom))

        path = QPainterPath()
        started = False
        peak_marks = []
        for t, v in self.points:
            if t < t0:
                continue
            pt = QPointF(x_of(t), y_of(v))
            if v > self.YMAX:
                peak_marks.append(pt)
            if started:
                path.lineTo(pt)
            else:
                path.moveTo(pt)
                started = True
        p.setPen(QPen(BLUE, 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(AMBER)
        for pt in peak_marks:
            p.drawEllipse(pt, 3, 3)
        p.end()


class StatusChip(QLabel):
    """A coloured dot and a short status text."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setContentsMargins(8, 2, 8, 2)
        self.set_status("-", "idle")

    def set_status(self, text: str, level: str = "ok", tip: str = "") -> None:
        c = LEVEL_COLORS.get(level, GREY).name()
        self.setText(f'<span style="color:{c}; font-size:14pt;">&#9679;</span>&nbsp;'
                     f'<b>{self.title}</b>&nbsp;{text}')
        self.setToolTip(tip or text)


class ArmButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("ARM", parent)
        self.setCheckable(True)
        self.setMinimumSize(150, 46)
        f = self.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.setFont(f)
        self.set_armed(False)

    def set_armed(self, armed: bool) -> None:
        self.blockSignals(True)
        self.setChecked(armed)
        self.blockSignals(False)
        if armed:
            self.setText("ARMED")
            self.setStyleSheet("QPushButton { background:#e5484d; color:white; border-radius:6px; "
                               "padding:6px 18px; } QPushButton:hover { background:#ec5d5e; }")
        else:
            self.setText("ARM")
            self.setStyleSheet("QPushButton { padding:6px 18px; }")


class _ThumbRelay(QObject):
    ready = Signal(str, QImage)


class _ThumbJob(QRunnable):
    def __init__(self, path, relay, size):
        super().__init__()
        self.path, self.relay, self.size = path, relay, size

    def run(self):
        r = QImageReader(self.path)
        r.setAutoTransform(True)
        s = r.size()
        if s.isValid():
            r.setScaledSize(s.scaled(self.size, Qt.AspectRatioMode.KeepAspectRatio))  # DCT-scaled decode
        img = r.read()
        self.relay.ready.emit(self.path, img)


class Gallery(QListWidget):
    """Latest photos and detector clips, newest first; double-click opens."""

    THUMB = QSize(168, 112)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(False)
        self.setIconSize(self.THUMB)
        self.setMovement(QListWidget.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(6)
        self.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setMinimumHeight(self.THUMB.height() + 48)
        self._items: dict = {}
        self._relay = _ThumbRelay()
        self._relay.ready.connect(self._on_thumb)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        blank = QPixmap(self.THUMB)
        blank.fill(QColor(40, 42, 48))
        self._blank = QIcon(blank)
        self.itemActivated.connect(self._open)

    def add_image(self, path: str, caption: str, tooltip: str = "", kind: str = "photo") -> None:
        if not path or path in self._items:
            return
        it = QListWidgetItem(self._blank, caption)
        it.setData(Qt.ItemDataRole.UserRole, path)
        it.setToolTip(tooltip or path)
        if kind == "clip":
            it.setForeground(QBrush(AMBER))
        elif kind == "lightning":
            it.setForeground(QBrush(GREEN))
        self.insertItem(0, it)
        self._items[path] = it
        while self.count() > 300:
            old = self.takeItem(self.count() - 1)
            self._items.pop(old.data(Qt.ItemDataRole.UserRole), None)
        self._pool.start(_ThumbJob(path, self._relay, self.THUMB))

    def _on_thumb(self, path: str, img: QImage) -> None:
        it = self._items.get(path)
        if it is not None and not img.isNull():
            it.setIcon(QIcon(QPixmap.fromImage(img)))

    def _open(self, item) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
