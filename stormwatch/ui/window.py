"""StormWatch main window."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import __version__
from .. import settings as settings_mod
from ..paths import output_root
from ..settings import Settings
from .client import EngineClient
from .widgets import AMBER, GREEN, RED, ArmButton, Gallery, PreviewView, SignalGraph, StatusChip

DETECTORS = [("auto", "Automatic"), ("webcam", "Webcam"), ("photodiode", "Photodiode (StormTrigger board)"),
             ("liveview", "D3300 live view (slowest)"), ("simulated", "Simulated storm (demo)"),
             ("none", "None")]
SHUTTERS = [("auto", "Automatic"), ("arduino", "StormTrigger remote cable"),
            ("line", "USB-serial remote cable (DTR/RTS)"), ("usb", "USB cable (PTP)"),
            ("none", "Test: never release")]
DOWNLOADS = [("jpeg", "JPEG only (NEF stays on card)"), ("all", "Everything"), ("none", "Leave on card")]
TARGETS = [("card", "Memory card"), ("ram", "Camera RAM (PC must download)")]
CAMERA_LABELS = {"shutterspeed": "Shutter speed", "f-number": "Aperture", "iso": "ISO",
                 "imagequality": "Image quality", "capturemode": "Release mode",
                 "whitebalance": "White balance", "exposurecompensation": "Exposure comp."}
CHECKLIST = """<h3>Before the storm</h3>
<ol>
<li><b>Lens switch on M</b>, focused at infinity (or on the horizon). Autofocus is the
biggest source of shutter delay, and it hunts in the dark.</li>
<li><b>Mode dial on M.</b> Set shutter, aperture and ISO for the light; the camera then
never pauses to meter.</li>
<li><b>SD card in the camera</b>, release mode Single (or Continuous with the remote cable
to get a burst per trigger). Image review off, auto-off timer long.</li>
<li>Point the <b>webcam (or photodiode) at the same patch of sky</b> as the D3300.</li>
<li><b>Arm</b>, then watch the signal graph: the blue line should idle well below the red
line and jump over it when the sky flashes.</li>
</ol>
<h3>What to expect</h3>
<p>Nothing on a computer beats the D3300's own shutter lag (~0.1 s), so the stroke that
<i>triggers</i> it is usually over before the shutter opens; you get the strokes that follow
and the lit cloud. The saved detector clip shows the triggering stroke. At night,
<b>Night mode</b> keeps the shutter open continuously and keeps only frames lightning
landed in - that is how you get the first stroke.</p>"""


def combo(items) -> QComboBox:
    cb = QComboBox()
    for key, label in items:
        cb.addItem(label, key)
    return cb


def set_combo(cb: QComboBox, key) -> None:
    i = cb.findData(key)
    if i < 0 and key not in (None, ""):
        cb.addItem(str(key), key)
        i = cb.count() - 1
    cb.setCurrentIndex(max(0, i))


def _dark_palette():
    from PySide6.QtGui import QPalette

    p = QPalette()
    roles = QPalette.ColorRole
    for role, c in [(roles.Window, (31, 33, 37)), (roles.WindowText, (222, 224, 228)),
                    (roles.Base, (22, 24, 27)), (roles.AlternateBase, (37, 39, 44)),
                    (roles.ToolTipBase, (37, 39, 44)), (roles.ToolTipText, (222, 224, 228)),
                    (roles.Text, (222, 224, 228)), (roles.Button, (42, 45, 50)),
                    (roles.ButtonText, (222, 224, 228)), (roles.BrightText, (255, 90, 90)),
                    (roles.Highlight, (61, 174, 233)), (roles.HighlightedText, (255, 255, 255)),
                    (roles.Link, (61, 174, 233)), (roles.PlaceholderText, (130, 132, 140))]:
        p.setColor(role, QColor(*c))
    for role in (roles.Text, roles.WindowText, roles.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(110, 112, 118))
    return p


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, demo: bool = False):
        super().__init__()
        self.settings = settings
        self.demo = demo
        self.qs = QSettings("StormWatch", "StormWatch")
        self.setWindowTitle("StormWatch" + (" - demo" if demo else ""))
        self.client = EngineClient(self)
        self.client.event.connect(self.on_event)
        self.client.preview.connect(self.on_preview)
        self.client.died.connect(self.on_died)

        self._armed = False
        self._detector_kind = "none"
        self._devices = {"video": [], "serial": []}
        self._cam_settings: dict = {}
        self._cam_widgets: dict = {}
        self._last_info = 0.0
        self._reactions: list = []
        self._clips = 0
        self._seen_errors: set = set()
        self._bind: dict = {}
        self._built = False

        self._build()
        self._built = True
        self._load_form(settings)
        self._restore()
        self.client.start(settings.to_dict())

    # ------------------------------------------------------------- layout ---
    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        # Top bar
        bar = QHBoxLayout()
        self.arm_btn = ArmButton()
        self.arm_btn.setToolTip("Arm / disarm  (Ctrl+Space)")
        self.arm_btn.clicked.connect(self.toggle_arm)
        self.test_btn = QPushButton("Test release")
        self.test_btn.setToolTip("Fire the shutter once now  (Ctrl+T)")
        self.test_btn.setMinimumHeight(46)
        self.test_btn.clicked.connect(lambda: self.client.send({"cmd": "test_fire"}))
        bar.addWidget(self.arm_btn)
        bar.addWidget(self.test_btn)
        bar.addSpacing(12)
        self.chip_detector = StatusChip("Detector")
        self.chip_shutter = StatusChip("Shutter")
        self.chip_camera = StatusChip("Camera")
        self.chip_mode = StatusChip("Mode")
        for c in (self.chip_detector, self.chip_shutter, self.chip_camera, self.chip_mode):
            bar.addWidget(c)
        bar.addStretch(1)
        self.rt_label = QLabel()
        self.rt_label.setStyleSheet("color: #8b8d98;")
        bar.addWidget(self.rt_label)
        root.addLayout(bar)

        # Banner for problems that need the user
        self.banner = QFrame()
        self.banner.setStyleSheet("QFrame { background:#4a3a12; border-radius:6px; } QLabel { color:#ffe1a3; }")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(10, 6, 10, 6)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_btn = QPushButton("Release camera")
        self.banner_btn.clicked.connect(lambda: self.client.send({"cmd": "release_camera"}))
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.banner_btn)
        self.banner.hide()
        root.addWidget(self.banner)

        # Centre: preview + graph | settings tabs
        centre = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.preview = PreviewView()
        self.graph = SignalGraph()
        ll.addWidget(self.preview, 5)
        ll.addWidget(self.graph, 1)
        centre.addWidget(left)
        centre.addWidget(self._build_tabs())
        centre.setStretchFactor(0, 3)
        centre.setStretchFactor(1, 1)
        centre.setSizes([1000, 430])
        self.centre = centre

        # Stats
        stats = QHBoxLayout()
        self.stat = {}
        for key, title in [("triggers", "Triggers"), ("photos", "Photos"), ("last", "Last trigger"),
                           ("reaction", "Frame to release"), ("release", "USB release"),
                           ("clips", "Clips"), ("night", "Night")]:
            box = QLabel()
            box.setTextFormat(Qt.TextFormat.RichText)
            self.stat[key] = (box, title)
            stats.addWidget(box)
            self._set_stat(key, "-")
        stats.addStretch(1)

        # Bottom: gallery | log
        bottom = QSplitter(Qt.Orientation.Horizontal)
        self.gallery = Gallery()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(800)
        bottom.addWidget(self.gallery)
        bottom.addWidget(self.log_view)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 2)

        main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(centre)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addLayout(stats)
        hl.addWidget(bottom)
        main.addWidget(holder)
        main.setStretchFactor(0, 4)
        main.setStretchFactor(1, 1)
        self.main_split = main
        root.addWidget(main, 1)
        self.setCentralWidget(central)

        self._build_menus()
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.toggle_arm)
        QShortcut(QKeySequence("Ctrl+T"), self, activated=lambda: self.client.send({"cmd": "test_fire"}))
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        a = QAction("Open photo folder", self)
        a.triggered.connect(self.open_folder)
        m.addAction(a)
        m.addSeparator()
        a = QAction("Quit", self)
        a.setShortcut(QKeySequence.StandardKey.Quit)
        a.triggered.connect(self.close)
        m.addAction(a)
        m = mb.addMenu("&View")
        self.dark_action = QAction("Dark theme (keeps night vision)", self, checkable=True)
        self.dark_action.toggled.connect(self.set_dark)
        m.addAction(self.dark_action)
        a = QAction("Full screen", self)
        a.setShortcut(QKeySequence("F11"))
        a.triggered.connect(self.toggle_fullscreen)
        m.addAction(a)
        m = mb.addMenu("&Help")
        a = QAction("Shooting checklist", self)
        a.triggered.connect(self.show_checklist)
        m.addAction(a)
        a = QAction("About StormWatch", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, "About StormWatch",
            f"<b>StormWatch {__version__}</b><br>Low-latency lightning trigger for Nikon DSLRs "
            "on Linux.<br><br>Detector to shutter in well under a millisecond on the PC; "
            "under 0.06 ms with the StormTrigger photodiode board."))
        m.addAction(a)

    def _scroll(self, w: QWidget) -> QScrollArea:
        for cb in w.findChildren(QComboBox):  # long items must not force the panel wide
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
        sa = QScrollArea()
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(w)
        return sa

    def _build_tabs(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._setup_tab()), "Setup")
        self.tabs.addTab(self._scroll(self._camera_tab()), "Camera")
        self.tabs.addTab(self._scroll(self._output_tab()), "Output")
        v.addWidget(self.tabs, 1)
        row = QHBoxLayout()
        self.dirty_label = QLabel("")
        self.dirty_label.setStyleSheet(f"color:{AMBER.name()};")
        self.revert_btn = QPushButton("Revert")
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setDefault(True)
        self.revert_btn.clicked.connect(lambda: self._load_form(self.settings))
        self.apply_btn.clicked.connect(self.apply)
        row.addWidget(self.dirty_label, 1)
        row.addWidget(self.revert_btn)
        row.addWidget(self.apply_btn)
        v.addLayout(row)
        box.setMinimumWidth(340)
        return box

    def _b(self, key, widget, kind):
        self._bind[key] = (widget, kind)
        sig = {"combo": "currentIndexChanged", "spin": "valueChanged", "dspin": "valueChanged",
               "check": "toggled", "line": "textChanged", "radio": "toggled"}[kind]
        getattr(widget, sig).connect(self._update_dirty)
        return widget

    def _setup_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        g = QGroupBox("Mode")
        gl = QVBoxLayout(g)
        self.mode_trigger = QRadioButton("Trigger")
        self.mode_night = QRadioButton("Night")
        self._b("mode", self.mode_trigger, "radio")
        for rb, text in ((self.mode_trigger, "Release the moment a flash is seen."),
                         (self.mode_night, "Back-to-back exposures; keep only frames lightning "
                                           "landed in. Catches the first stroke.")):
            gl.addWidget(rb)
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.setContentsMargins(24, 0, 0, 4)
            lab.setStyleSheet("color: #8b8d98;")
            gl.addWidget(lab)
        nf = QFormLayout()
        self.night_exp = self._b("night_exposure_s", QDoubleSpinBox(), "dspin")
        self.night_exp.setRange(0, 30)
        self.night_exp.setDecimals(2)
        self.night_exp.setSpecialValueText("from camera")
        self.night_exp.setSuffix(" s")
        self.night_gap = self._b("night_gap_s", QDoubleSpinBox(), "dspin")
        self.night_gap.setRange(0.1, 10)
        self.night_gap.setSuffix(" s")
        nf.addRow("Night exposure", self.night_exp)
        nf.addRow("Pause between", self.night_gap)
        gl.addLayout(nf)
        v.addWidget(g)

        g = QGroupBox("Detector")
        f = QFormLayout(g)
        self.det_combo = self._b("detector", combo(DETECTORS), "combo")
        f.addRow("Detector", self.det_combo)
        row = QHBoxLayout()
        self.video_combo = self._b("video_device", QComboBox(), "combo")
        self.video_combo.currentIndexChanged.connect(self._fill_modes)
        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Rescan webcams and serial ports")
        refresh.clicked.connect(lambda: self.client.send({"cmd": "devices"}))
        row.addWidget(self.video_combo, 1)
        row.addWidget(refresh)
        f.addRow("Webcam", row)
        self.mode_combo = self._b("video_mode", QComboBox(), "combo")
        f.addRow("Webcam mode", self.mode_combo)
        self.exposure_spin = self._b("webcam_exposure", QSpinBox(), "spin")
        self.exposure_spin.setRange(0, 10000)
        self.exposure_spin.setSpecialValueText("auto (frame rate held)")
        self.exposure_spin.setToolTip("Manual webcam exposure in 100 us units; lock it if "
                                      "auto exposure pumps")
        f.addRow("Webcam exposure", self.exposure_spin)
        srow = QHBoxLayout()
        self.sens = QSlider(Qt.Orientation.Horizontal)
        self.sens.setRange(0, 100)
        self.sens_label = QLabel()
        self.sens_label.setMinimumWidth(32)
        self._sens_timer = QTimer(self)
        self._sens_timer.setSingleShot(True)
        self._sens_timer.setInterval(150)
        self._sens_timer.timeout.connect(self._send_sensitivity)
        self.sens.valueChanged.connect(lambda v: (self.sens_label.setText(str(v)), self._sens_timer.start()))
        srow.addWidget(self.sens, 1)
        srow.addWidget(self.sens_label)
        f.addRow("Sensitivity", srow)
        hint = QLabel("Live: the blue line in the graph must cross the red line for a release.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b8d98;")
        f.addRow(hint)
        v.addWidget(g)

        g = QGroupBox("Shutter")
        f = QFormLayout(g)
        self.shutter_combo = self._b("shutter", combo(SHUTTERS), "combo")
        f.addRow("Release via", self.shutter_combo)
        self.serial_combo = self._b("serial_port", QComboBox(), "combo")
        f.addRow("Board / cable port", self.serial_combo)
        self.hold = self._b("hold_ms", QSpinBox(), "spin")
        self.hold.setRange(20, 5000)
        self.hold.setSuffix(" ms")
        self.hold.setToolTip("How long the remote button is held. Longer = a burst in Continuous mode.")
        f.addRow("Button hold", self.hold)
        self.cooldown = self._b("cooldown_ms", QSpinBox(), "spin")
        self.cooldown.setRange(50, 60000)
        self.cooldown.setSuffix(" ms")
        f.addRow("Minimum gap", self.cooldown)
        self.awake = self._b("keep_awake", QCheckBox("Hold half-press while armed (faster release)"), "check")
        f.addRow(self.awake)
        self.pd_thr = self._b("photodiode_threshold", QSpinBox(), "spin")
        self.pd_thr.setRange(1, 250)
        self.pd_k = self._b("photodiode_k", QSpinBox(), "spin")
        self.pd_k.setRange(1, 40)
        f.addRow("Photodiode min rise", self.pd_thr)
        f.addRow("Photodiode x noise", self.pd_k)
        self.pd_assist = self._b("photodiode_assist",
                                 QCheckBox("Photodiode also fires (with webcam detection)"), "check")
        f.addRow(self.pd_assist)
        self.fast_usb = self._b("fast_usb_release", QCheckBox("One-transaction USB release"), "check")
        f.addRow(self.fast_usb)
        v.addWidget(g)
        v.addStretch(1)
        return w

    def _camera_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Nikon D3300")
        self.cam_grid = QGridLayout(g)
        self.cam_status = {}
        for i, (key, title) in enumerate([("model", "Body"), ("batterylevel", "Battery"),
                                          ("expprogram", "Mode dial"), ("focusmode", "Focus"),
                                          ("lensname", "Lens"), ("port", "USB port")]):
            self.cam_grid.addWidget(QLabel(title), i, 0)
            lab = QLabel("-")
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.cam_grid.addWidget(lab, i, 1)
            self.cam_status[key] = lab
        v.addWidget(g)
        self.cam_warn = QLabel()
        self.cam_warn.setWordWrap(True)
        self.cam_warn.setStyleSheet(f"color:{AMBER.name()};")
        v.addWidget(self.cam_warn)

        g = QGroupBox("Exposure (applied to the camera immediately)")
        self.cam_form = QFormLayout(g)
        for key, title in CAMERA_LABELS.items():
            cb = QComboBox()
            cb.setEnabled(False)
            cb.activated.connect(lambda _i, k=key, c=cb: self.client.send(
                {"cmd": "camera_set", "key": k, "value": c.currentText()}))
            self._cam_widgets[key] = cb
            self.cam_form.addRow(title, cb)
        btn = QPushButton("Read settings from camera")
        btn.clicked.connect(lambda: self.client.send({"cmd": "camera_refresh"}))
        self.cam_form.addRow(btn)
        v.addWidget(g)

        g = QGroupBox("Files")
        f = QFormLayout(g)
        self.target_combo = self._b("capture_target", combo(TARGETS), "combo")
        self.download_combo = self._b("download", combo(DOWNLOADS), "combo")
        f.addRow("Save in camera to", self.target_combo)
        f.addRow("Copy to this PC", self.download_combo)
        v.addWidget(g)
        v.addStretch(1)
        return w

    def _output_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Where things go")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.out_edit = self._b("output_dir", QLineEdit(), "line")
        self.out_edit.setPlaceholderText(output_root(""))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.out_edit, 1)
        row.addWidget(browse)
        f.addRow("Folder", row)
        self.session_label = QLabel("-")
        self.session_label.setWordWrap(True)
        f.addRow("Tonight", self.session_label)
        open_btn = QPushButton("Open tonight's folder")
        open_btn.clicked.connect(self.open_folder)
        f.addRow(open_btn)
        self.clips_check = self._b("save_clips", QCheckBox("Save detector frames around each trigger"), "check")
        f.addRow(self.clips_check)
        v.addWidget(g)
        g = QGroupBox("Performance")
        f = QFormLayout(g)
        self.rt_check = self._b("realtime", QCheckBox("Real-time priority for detection"), "check")
        f.addRow(self.rt_check)
        self.rt_detail = QLabel("-")
        self.rt_detail.setWordWrap(True)
        f.addRow("Scheduling", self.rt_detail)
        v.addWidget(g)
        v.addStretch(1)
        return w

    # -------------------------------------------------------------- form ---
    def _load_form(self, s: Settings) -> None:
        for key, (wdg, kind) in self._bind.items():
            wdg.blockSignals(True)
        try:
            set_combo(self.det_combo, s.detector)
            set_combo(self.shutter_combo, s.shutter)
            set_combo(self.target_combo, s.capture_target)
            set_combo(self.download_combo, s.download)
            self._fill_devices(s)
            self.mode_trigger.setChecked(s.mode != "night")
            self.mode_night.setChecked(s.mode == "night")
            self.night_exp.setValue(s.night_exposure_s)
            self.night_gap.setValue(s.night_gap_s)
            self.exposure_spin.setValue(s.webcam_exposure)
            self.hold.setValue(s.hold_ms)
            self.cooldown.setValue(s.cooldown_ms)
            self.awake.setChecked(s.keep_awake)
            self.pd_thr.setValue(s.photodiode_threshold)
            self.pd_k.setValue(s.photodiode_k)
            self.pd_assist.setChecked(s.photodiode_assist)
            self.fast_usb.setChecked(s.fast_usb_release)
            self.out_edit.setText(s.output_dir)
            self.clips_check.setChecked(s.save_clips)
            self.rt_check.setChecked(s.realtime)
        finally:
            for key, (wdg, kind) in self._bind.items():
                wdg.blockSignals(False)
        self.sens.blockSignals(True)
        self.sens.setValue(int(round(s.sensitivity * 100)))
        self.sens_label.setText(str(self.sens.value()))
        self.sens.blockSignals(False)
        self._update_dirty()

    def _form(self) -> Settings:
        d = self.settings.to_dict()
        d.update({
            "mode": "night" if self.mode_night.isChecked() else "trigger",
            "night_exposure_s": self.night_exp.value(), "night_gap_s": self.night_gap.value(),
            "detector": self.det_combo.currentData(), "video_device": self.video_combo.currentData() or "",
            "video_mode": self.mode_combo.currentData() or "auto",
            "webcam_exposure": self.exposure_spin.value(), "shutter": self.shutter_combo.currentData(),
            "serial_port": self.serial_combo.currentData() or "", "hold_ms": self.hold.value(),
            "cooldown_ms": self.cooldown.value(), "keep_awake": self.awake.isChecked(),
            "photodiode_threshold": self.pd_thr.value(), "photodiode_k": self.pd_k.value(),
            "photodiode_assist": self.pd_assist.isChecked(), "fast_usb_release": self.fast_usb.isChecked(),
            "capture_target": self.target_combo.currentData(), "download": self.download_combo.currentData(),
            "output_dir": self.out_edit.text().strip(), "save_clips": self.clips_check.isChecked(),
            "realtime": self.rt_check.isChecked(), "sensitivity": self.sens.value() / 100.0,
        })
        return Settings.from_dict(d)

    def _update_dirty(self, *_):
        if not self._built:
            return
        a, b = self._form().to_dict(), self.settings.to_dict()
        a.pop("sensitivity"), b.pop("sensitivity")
        dirty = a != b
        self.apply_btn.setEnabled(dirty)
        self.revert_btn.setEnabled(dirty)
        self.dirty_label.setText("Unsaved changes" if dirty else "")

    def apply(self) -> None:
        s = self._form()
        if self.demo:
            s.detector, s.shutter = "simulated", "none"
        else:
            settings_mod.save(s)
        self.settings = s
        self.client.send({"cmd": "apply", "settings": s.to_dict()})
        self._update_dirty()
        self._log("info", "settings applied")

    def _send_sensitivity(self) -> None:
        v = self.sens.value() / 100.0
        self.settings.sensitivity = v
        if not self.demo:
            settings_mod.save(self.settings)
        self.client.send({"cmd": "sensitivity", "value": v})

    def _fill_devices(self, s: Settings | None = None) -> None:
        s = s or self._form()
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        self.video_combo.addItem("First webcam found", "")
        for d in self._devices["video"]:
            tag = ", built-in" if d.get("internal") else ""
            self.video_combo.addItem(f"{d['name']}  ({os.path.basename(d['path'])}{tag})", d["path"])
        set_combo(self.video_combo, s.video_device)
        self.video_combo.blockSignals(False)
        self._fill_modes(mode=s.video_mode)
        self.serial_combo.blockSignals(True)
        self.serial_combo.clear()
        self.serial_combo.addItem("Find the StormTrigger board", "")
        for p in self._devices["serial"]:
            self.serial_combo.addItem(p["label"], p["path"])
        set_combo(self.serial_combo, s.serial_port)
        self.serial_combo.blockSignals(False)

    def _fill_modes(self, *_args, mode=None) -> None:
        mode = mode or self.mode_combo.currentData() or "auto"
        path = self.video_combo.currentData() or (self._devices["video"][0]["path"]
                                                  if self._devices["video"] else "")
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItem("Automatic (fastest)", "auto")
        for d in self._devices["video"]:
            if d["path"] == path:
                for m in d.get("modes", []):
                    self.mode_combo.addItem(m, m)
        set_combo(self.mode_combo, mode)
        self.mode_combo.blockSignals(False)
        self._update_dirty()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Photo folder", self.out_edit.text() or output_root(""))
        if d:
            self.out_edit.setText(d)

    # ------------------------------------------------------------ actions ---
    def toggle_arm(self) -> None:
        self.client.send({"cmd": "disarm" if self._armed else "arm"})
        self.arm_btn.set_armed(self._armed)  # the engine's state event decides

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def open_folder(self) -> None:
        path = self.session_label.text()
        if not os.path.isdir(path):
            path = output_root(self.settings.output_dir)
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def show_checklist(self) -> None:
        m = QMessageBox(self)
        m.setWindowTitle("Shooting checklist")
        m.setTextFormat(Qt.TextFormat.RichText)
        m.setText(CHECKLIST)
        m.exec()

    def set_dark(self, on: bool) -> None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if on:
            if not hasattr(self, "_orig_palette"):
                self._orig_palette = app.palette()
                self._orig_style = app.style().name()
            app.setStyle("Fusion")
            app.setPalette(_dark_palette())
        elif hasattr(self, "_orig_palette"):
            app.setStyle(self._orig_style)
            app.setPalette(self._orig_palette)
        self.qs.setValue("dark", on)

    # ------------------------------------------------------------- events ---
    def _set_stat(self, key: str, value: str) -> None:
        box, title = self.stat[key]
        box.setText(f'<span style="color:#8b8d98">{title}</span>&nbsp;<b>{value}</b>'
                    "&nbsp;&nbsp;&nbsp;")

    def _log(self, level: str, msg: str) -> None:
        color = {"error": RED.name(), "warn": AMBER.name(), "trigger": GREEN.name()}.get(level)
        stamp = time.strftime("%H:%M:%S")
        text = msg.replace("&", "&amp;").replace("<", "&lt;")
        if color:
            self.log_view.appendHtml(f'<span style="color:{color}">{stamp}  {text}</span>')
        else:
            self.log_view.appendHtml(f"{stamp}  {text}")

    def _show_banner(self, text: str | None, button: bool = False) -> None:
        if not text:
            self.banner.hide()
            return
        self.banner_text.setText(text)
        self.banner_btn.setVisible(button)
        self.banner.show()

    def on_preview(self, img, flags) -> None:
        self.preview.set_image(img, flags)

    def on_died(self) -> None:
        self._show_banner("The engine stopped unexpectedly. See the log; restarting it...")
        self._log("error", "engine process exited; restarting")
        QTimer.singleShot(1500, lambda: self.client.start(self.settings.to_dict()))

    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        handler = getattr(self, f"_ev_{t}", None)
        if handler:
            handler(ev)

    def _ev_state(self, ev) -> None:
        self._armed = ev["armed"]
        self.arm_btn.set_armed(self._armed)
        self.preview.set_armed(self._armed)
        self.graph.armed = self._armed
        det, sh = ev["detector"], ev["shutter"]
        self._detector_kind = det["kind"]
        label = dict(DETECTORS).get(det["kind"], det["kind"])
        if det["error"]:
            self.chip_detector.set_status("error", "error", det["error"])
        elif det["kind"] == "none":
            self.chip_detector.set_status("none", "warn", "Choose a detector in Setup")
        else:
            short = label.split(" (")[0]
            if "@" in det["mode"]:
                short += f"  {det['mode'].rsplit('@', 1)[1]} fps"
            self.chip_detector.set_status(short, "ok", f"{det['name']}\n{det['mode']}")
        if sh["error"]:
            self.chip_shutter.set_status("error", "error", sh["error"])
        else:
            self.chip_shutter.set_status(dict(SHUTTERS).get(sh["kind"], sh["kind"]).split(" (")[0],
                                         "warn" if sh["kind"] == "none" else "ok")
        night = ev["mode"] == "night"
        self.chip_mode.set_status("Night" if night else "Trigger", "info")
        self.rt_label.setText(ev["rt"])
        self.rt_detail.setText(ev["rt"])
        self.session_label.setText(ev["session_dir"])
        for key in ("detector", "shutter"):
            err = ev[key]["error"]
            if err and err not in self._seen_errors:
                self._seen_errors.add(err)
                self._log("error", f"{key}: {err}")
        for note in ev.get("notes", []):
            if note not in self._seen_errors:
                self._seen_errors.add(note)
                self._log("info", note)
        if det["kind"] == "photodiode":
            self.preview.clear_image("Photodiode detector: there is no picture.\n"
                                     "The graph below shows the sensor; the board fires the "
                                     "camera by itself in under 0.06 ms.")
        elif det["kind"] == "none":
            self.preview.clear_image("No detector. Connect a webcam or the StormTrigger board, "
                                     "or pick one in Setup.")
        self.arm_btn.setEnabled(det["kind"] != "none" and not det["error"])
        self._set_stat("triggers", str(ev["triggers"]))
        self._set_stat("photos", str(ev["photos"]))

    def _ev_devices(self, ev) -> None:
        self._devices = {"video": ev["video"], "serial": ev["serial"]}
        cur = self._form()
        self._fill_devices(cur)

    def _ev_telemetry(self, ev) -> None:
        self.graph.add_points(ev["points"])
        now = time.monotonic()
        if now - self._last_info > 0.25:
            self._last_info = now
            if self._detector_kind == "photodiode":
                self.preview.set_info("")
            else:
                self.preview.set_info(f"{ev['fps']:.0f} fps   frame age {ev['age_ms']:.1f} ms   "
                                      f"detect {ev['proc_ms']:.2f} ms")

    def _ev_trigger(self, ev) -> None:
        self.graph.add_trigger(ev["t_sent"])
        if ev.get("hardware"):
            detail = "in-board"
        elif ev.get("reaction_ms") is not None:
            detail = f"{ev['reaction_ms']:.1f} ms"
            self._reactions.append(ev["reaction_ms"])
            self._reactions = self._reactions[-50:]
            avg = sum(self._reactions) / len(self._reactions)
            self._set_stat("reaction", f"{ev['reaction_ms']:.1f} ms (avg {avg:.1f})")
        else:
            detail = "test"
        if ev.get("hardware"):
            self._set_stat("reaction", "< 0.06 ms (board)")
        self.preview.show_trigger(f"⚡ TRIGGER #{ev['id']}   {detail}")
        self._set_stat("triggers", str(ev["id"]))
        self._set_stat("last", time.strftime("%H:%M:%S", time.localtime(ev["wall"])))
        self._log("trigger", f"trigger #{ev['id']} from {ev['source']} via {ev['shutter']} ({detail})")

    def _ev_camera_fired(self, ev) -> None:
        ms = (ev["t_accepted"] - ev["t_request"]) * 1000
        self._set_stat("release", f"{ev['method']} {ms:.1f} ms")

    def _ev_file(self, ev) -> None:
        self.gallery.add_image(ev["path"], ev["name"], f"{ev['path']}\ncopied in {ev['seconds']} s")
        self._log("info", f"photo {ev['name']} saved")

    def _ev_clip(self, ev) -> None:
        self._clips += 1
        self._set_stat("clips", str(self._clips))
        if ev.get("trigger_frame"):
            self.gallery.add_image(ev["trigger_frame"], f"detector #{ev['trigger_id']}",
                                   f"Detector frames for trigger #{ev['trigger_id']}\n{ev['dir']}", kind="clip")

    def _ev_night_flash(self, ev) -> None:
        self.graph.add_flash(time.monotonic())

    def _ev_night_file(self, ev) -> None:
        self._set_stat("night", f"{ev['kept']} kept / {ev['kept'] + ev['rejected']}")
        if ev["lightning"]:
            self.gallery.add_image(ev["path"], "⚡ " + os.path.basename(ev["path"]), kind="lightning")

    def _ev_camera(self, ev) -> None:
        state = ev.get("state", "")
        model = ev.get("model", "")
        status = ev.get("status", {})
        if state == "ready":
            batt = status.get("batterylevel")
            text = model.replace("Nikon DSC ", "") or "camera"
            self.chip_camera.set_status(f"{text}{'  ' + batt if batt else ''}",
                                        "warn" if ev.get("warnings") else "ok",
                                        "\n".join(ev.get("warnings", [])) or model)
            self._show_banner(None)
        elif state == "blocked":
            holders = ", ".join(f"{h['name']} ({h['pid']})" for h in ev.get("holders", [])) or "another program"
            self.chip_camera.set_status("held by another app", "error")
            self._show_banner(f"The D3300 is held by {holders}. KDE/GNOME photo import grabs "
                              "cameras when they are plugged in.", button=True)
        elif state == "no-permission":
            self.chip_camera.set_status("no permission", "error", ev.get("error", ""))
            self._show_banner(ev.get("error", ""))
        elif state == "searching":
            self.chip_camera.set_status("not connected", "idle", "Plug the D3300 in over USB and switch it on")
            self._show_banner(None)
            self.cam_warn.setText("Not connected. Plug the D3300 in with its USB cable and switch it "
                                  "on; it is found automatically.")
            for cb in self._cam_widgets.values():
                cb.setEnabled(False)
        elif state == "error":
            self.chip_camera.set_status("error", "error", ev.get("error", ""))
        if "model" in ev:
            self.cam_status["model"].setText(model.replace("Nikon DSC ", "Nikon ") or "-")
        if "port" in ev:
            self.cam_status["port"].setText(ev["port"])
        for key, lab in self.cam_status.items():
            if key in status:
                lab.setText(str(status[key]))
        if "warnings" in ev:
            self.cam_warn.setText("\n".join("⚠ " + w for w in ev["warnings"]))
        for key, d in ev.get("settings", {}).items():
            cb = self._cam_widgets.get(key)
            if cb is None:
                continue
            cb.blockSignals(True)
            cb.clear()
            for c in d.get("choices", []) or [d["value"]]:
                cb.addItem(str(c))
            cb.setCurrentText(str(d["value"]))
            cb.setEnabled(not d.get("readonly"))
            cb.blockSignals(False)

    def _ev_log(self, ev) -> None:
        self._log(ev["level"], ev["msg"])

    # ------------------------------------------------------------ window ---
    def _restore(self) -> None:
        geo = self.qs.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1360, 860)
        if self.qs.value("dark", False, type=bool):
            self.dark_action.setChecked(True)

    def closeEvent(self, e) -> None:
        if self._armed:
            r = QMessageBox.question(self, "StormWatch is armed",
                                     "Disarm and quit? Lightning will not be photographed.")
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.qs.setValue("geometry", self.saveGeometry())
        self.client.stop()
        e.accept()
