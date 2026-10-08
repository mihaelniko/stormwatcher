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
