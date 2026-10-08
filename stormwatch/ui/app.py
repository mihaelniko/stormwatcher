"""Application entry point for the window."""
from __future__ import annotations

import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..settings import Settings

ICON = os.path.join(os.path.dirname(__file__), "stormwatch.svg")


def run(settings: Settings, demo: bool = False) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("StormWatch")
    app.setOrganizationName("StormWatch")
    app.setDesktopFileName("stormwatch")  # ties the window to stormwatch.desktop in KDE
    app.setWindowIcon(QIcon(ICON))
    from .window import MainWindow

    win = MainWindow(settings, demo=demo)
    win.show()
    # Let Ctrl+C in the terminal close the window cleanly.
    signal.signal(signal.SIGINT, lambda *a: win.close())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(250)
    sys.exit(app.exec())
