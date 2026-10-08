"""The window, offscreen, driving a real engine process in demo mode."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_window_runs_demo_arms_and_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from PySide6.QtCore import QEventLoop, QTimer

    from stormwatch.settings import Settings
    from stormwatch.ui.window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    s = Settings(detector="simulated", shutter="none", output_dir=str(tmp_path / "out"), realtime=False)
    win = MainWindow(s, demo=True)
    win.show()

    def spin(seconds, until=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            loop = QEventLoop()
            QTimer.singleShot(20, loop.quit)
            loop.exec()
            if until and until():
                return True
        return bool(until and until())

    try:
        assert spin(15, lambda: win.preview._img is not None and len(win.graph.points) > 30)
        assert win.chip_detector.text().count("Simulated")
        win.toggle_arm()
        assert spin(5, lambda: win._armed)
        assert win.arm_btn.text() == "ARMED"
        win.dark_action.setChecked(True)
        win.sens.setValue(40)
        assert spin(1, lambda: abs(win.settings.sensitivity - 0.4) < 1e-6)
        win.night_gap.setValue(1.5)
        assert win.apply_btn.isEnabled()
        win.apply()
        assert spin(8, lambda: win._armed)  # apply keeps it armed
        win.toggle_arm()
        assert spin(5, lambda: not win._armed)
    finally:
        win.close()
        app.processEvents()
    assert win.client.proc is None


def _spin(seconds, until=None):
    from PySide6.QtCore import QEventLoop, QTimer

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        if until and until():
            return True
    return bool(until and until())


def test_night_mode_arms_without_apply_and_shows_progress(tmp_path, monkeypatch):
    """The user's path: pick Night, press ARM straight away (no Apply)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from stormwatch.settings import Settings
    from stormwatch.ui.window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(Settings(detector="simulated", shutter="none", output_dir=str(tmp_path / "out"),
                              realtime=False), demo=True)
    win.show()
    try:
        assert _spin(15, lambda: len(win.graph.points) > 30)
        win.mode_night.setChecked(True)
        win.arm_btn.click()
        assert _spin(8, lambda: win._armed)
        assert _spin(8, lambda: any(e[2] != "open" for e in win.graph.exposures.values()))
        assert "exposures" in win.stat["night"][0].text()
    finally:
        win._armed = False
        win.close()
        app.processEvents()


def test_unarmable_night_mode_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from stormwatch.settings import Settings
    from stormwatch.ui.window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # USB release, night mode, no camera connected, exposure "from camera".
    win = MainWindow(Settings(detector="simulated", shutter="usb", mode="night",
                              output_dir=str(tmp_path / "out"), realtime=False), demo=False)
    win.show()
    try:
        assert _spin(15, lambda: len(win.graph.points) > 10)
        win.arm_btn.click()
        assert _spin(8, lambda: getattr(win, "_arm_box", None) is not None)
        assert win._arm_box.isVisible() and "exposure" in win._arm_box.text()
        assert not win._armed and win.arm_btn.text() == "ARM"
    finally:
        win.close()
        app.processEvents()
