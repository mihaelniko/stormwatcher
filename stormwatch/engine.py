"""The StormWatch engine: detection -> shutter, plus everything around it.

Hot path (one thread, real-time priority where available):

    frame from the driver -> FlashDetector (~0.4 ms) -> shutter.fire()

Only after the release is issued does the thread spend time on clips, the
preview and telemetry. The photodiode path does not touch the PC at all: the
StormTrigger board fires in its ADC interrupt and only reports afterwards.

The engine runs in its own process under the UI (see process.py) or directly
in headless mode. It talks to the outside through ``emit(dict)`` and
``handle(dict)``.
"""
from __future__ import annotations

import os
import threading
import time

from . import paths, rt
from .arduino import ArduinoError, ArduinoLink
from .clips import ClipRecorder
from .detector import FlashDetector
from .night import NightController
from .pixfmt import JPEG_FORMATS, to_rgb
from .settings import Settings
from .shutter import ArduinoShutter, LineShutter, NullShutter, Shutter, UsbShutter
from .simulate import SimulatedSource
from .tty import list_ports
from .v4l2 import Mode, V4L2Camera, choose_mode, list_devices

TELEMETRY_PERIOD = 0.05
PREVIEW_FPS = 15.0


class PreviewPublisher:
    """Turns the latest offered frame into an RGB preview in shared memory,
    on its own thread, at most PREVIEW_FPS times a second."""

    def __init__(self, preview):
        self.preview = preview
        self.enabled = True
        self._slot = None
        self._evt = threading.Event()
        self._last = 0.0
        self._stop = False
        self._t = threading.Thread(target=self._run, name="preview", daemon=True)
        self._t.start()

    def offer(self, frame, flash: bool) -> None:
        now = time.monotonic()
        if not self.enabled or now - self._last < 1.0 / PREVIEW_FPS and not flash:
            return
        self._last = now
        if frame.raw is not None:
            data = bytes(frame.raw)
        else:
            data = frame.luma.tobytes() if frame.luma.flags.c_contiguous else frame.luma.copy().tobytes()
        self._slot = (data, frame.fmt if frame.raw is not None else "GREY",
                      frame.width or frame.luma.shape[1], frame.height or frame.luma.shape[0],
                      frame.stride if frame.raw is not None else frame.luma.shape[1], flash)
        self._evt.set()

    def stop(self) -> None:
        self._stop = True
        self._evt.set()

    def _run(self) -> None:
        import numpy as np

        while True:
            self._evt.wait()
            self._evt.clear()
            if self._stop:
                return
            slot, self._slot = self._slot, None
            if slot is None:
                continue
            data, fmt, w, h, stride, flash = slot
            try:
                if fmt in JPEG_FORMATS:
                    from io import BytesIO

                    from PIL import Image

                    im = Image.open(BytesIO(data))
                    im.draft("RGB", (im.size[0] // 2, im.size[1] // 2) if im.size[0] > 960 else im.size)
                    img = np.asarray(im.convert("RGB"))
                elif fmt == "GREY":
                    img = np.frombuffer(data, np.uint8)[: stride * h].reshape(h, stride)[:, :w]
                else:
                    img = to_rgb(data, fmt, w, h, stride)
                while img.shape[1] > 960 or img.shape[0] > 720:
                    img = img[::2, ::2]
                self.preview.write(img, flags=1 if flash else 0)
            except Exception:
                continue


class Engine:
    def __init__(self, settings: Settings, emit, *, preview=None, gp=None, enable_camera=True,
                 camera_factory=None, link_factory=None, video_factory=None, sim_factory=None,
                 list_video=None, list_serial=None):
        self.s = settings
        self._emit = emit
        self.preview = PreviewPublisher(preview) if preview is not None else None
        self._gp = gp
        self._enable_camera = enable_camera
        self._camera_factory = camera_factory
        self._link_factory = link_factory or (lambda path, **kw: ArduinoLink(path, **kw))
        self._video_factory = video_factory or (lambda path: V4L2Camera(path))
        self._sim_factory = sim_factory or (lambda: SimulatedSource())
        self._list_video = list_video or list_devices
        self._list_serial = list_serial or list_ports

        self.camera = None
        self.link: ArduinoLink | None = None
        self._link_port = ""
        self.shutter: Shutter = NullShutter()
        self.detector = FlashDetector(settings.sensitivity)
        self.detector_kind = "none"
        self.detector_name = ""
        self.detector_mode = ""
        self.errors: dict = {}
        self.notes: list = []
        self.armed = False
        self.night: NightController | None = None
        self.clips: ClipRecorder | None = None
        self.rt_desc = "normal priority"
        self.session_dir = ""
        self.trigger_count = 0
        self.photo_count = 0
        self._last_fire = -1e9
        self._fire_lock = threading.Lock()
        self._src_thread = None
        self._src_stop = threading.Event()
        self._source = None
        self._tele: list = []
        self._tele_last = 0.0
        self._fps = 0.0
        self._t_prev_frame = None
        self._age = 0.0
        self._proc = 0.0
        self._level = 0.0
        self._pd_saturated = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------- output ---
    def emit(self, ev: dict) -> None:
        try:
            self._emit(ev)
        except Exception:
            pass

    def log(self, level: str, msg: str) -> None:
        self.emit({"type": "log", "level": level, "msg": msg})

    def state(self) -> dict:
        cam = self.camera
        return {"type": "state", "armed": self.armed, "mode": self.s.mode,
                "detector": {"kind": self.detector_kind, "name": self.detector_name,
                             "mode": self.detector_mode, "error": self.errors.get("detector")},
                "shutter": {"kind": self.shutter.kind, "label": self.shutter.label,
                            "error": self.errors.get("shutter")},
                "arduino": {"port": self._link_port if self.link else "",
                            "board": getattr(self.link, "board", "")},
                "camera": {"state": cam.state if cam else "off", "model": cam.model if cam else ""},
                "rt": self.rt_desc, "session_dir": self.session_dir, "notes": self.notes,
                "triggers": self.trigger_count, "photos": self.photo_count,
                "settings": self.s.to_dict()}

    def emit_state(self) -> None:
        self.emit(self.state())

    def _video_modes(self, path: str) -> list[str]:
        """Best modes first (fps, then no-decode formats, then near VGA)."""
        if self._source is not None and getattr(self._source, "path", None) == path:
            src = self._source  # already streaming: ask the open device
            modes = src.modes()
        else:
            try:
                with self._video_factory(path) as cam:
                    modes = cam.modes()
            except OSError:
                return []
        ranked = []
        while modes and len(ranked) < 16:
            m = choose_mode(modes)
            if m is None:
                break
            ranked.append(str(m))
            modes = [x for x in modes if x != m]
        return ranked

    def devices(self) -> dict:
        vids = [{"path": d.path, "name": d.name, "stable": d.stable_path,
                 "internal": getattr(d, "internal", False),
                 "modes": self._video_modes(d.path)} for d in self._list_video()]
        ports = [{"path": p.path, "label": p.label, "arduino": p.likely_arduino, "stable": p.stable_path}
                 for p in self._list_serial()]
        return {"type": "devices", "video": vids, "serial": ports}

    # ---------------------------------------------------------- lifecycle ---
    def start(self) -> None:
        if threading.current_thread() is threading.main_thread():
            rt.install_handlers()
        self.session_dir = paths.session_dir(paths.output_root(self.s.output_dir))
        os.makedirs(self.session_dir, exist_ok=True)
        if self._enable_camera:
            self._start_camera()
        self.emit(self.devices())
        self._configure()

    def shutdown(self) -> None:
        self.disarm()
        self._teardown(keep_link=False)
        if self.camera:
            self.camera.stop()
            self.camera = None
        if self.preview:
            self.preview.stop()

    def _start_camera(self) -> None:
        try:
            if self._camera_factory:
                cam = self._camera_factory(self._on_camera_event)
            else:
                from .camera import CameraWorker

                cam = CameraWorker(self._on_camera_event, gp=self._gp, download=self.s.download,
                                   dest_dir=self.session_dir, capture_target=self.s.capture_target,
                                   fast_trigger=self.s.fast_usb_release)
        except ImportError:
            self.log("warn", "python3-gphoto2 is not installed: no USB camera control")
            return
        cam.on_file = self._on_camera_file
        cam.start()
        self.camera = cam

    def _on_camera_event(self, ev: dict) -> None:
        if ev.get("type") == "file":
            self.photo_count += 1
        self.emit(ev)

    def _on_camera_file(self, path, stem, t_added) -> None:
        night = self.night
        if night is not None:
            night.on_file(path, stem, t_added)

    # -------------------------------------------------------- configuring ---
    def _resolve(self) -> tuple[str, str]:
        det, sh = self.s.detector, self.s.shutter
        link = None
        if det in ("auto", "photodiode") or sh in ("auto", "arduino"):
            link = self._find_link()  # probed once per configure
        if det == "auto":
            if self._list_video():
                det = "webcam"
            elif link is not None:
                det = "photodiode"
            else:
                det = "liveview" if self.camera else "none"
        if sh == "auto":
            sh = "arduino" if link is not None else ("usb" if self.camera else "none")
        return det, sh

    def _find_link(self):
        """Open (or reuse) the StormTrigger board. Returns the link or None."""
        want = self.s.serial_port
        if self.link and self.link.connected and (not want or want == self._link_port):
            return self.link
        if self.link:
            self.link.close()
            self.link = None
        candidates = [want] if want else [p.path for p in self._list_serial() if p.likely_arduino]
        for path in candidates:
            try:
                link = self._link_factory(path, on_fire=self._on_board_fire,
                                          on_telemetry=self._on_board_telemetry,
                                          on_log=self.log)
                link.open()
                self.link, self._link_port = link, path
                self.log("info", f"StormTrigger board on {path}")
                return link
            except (ArduinoError, OSError) as e:
                if want:
                    self.errors["shutter"] = str(e)
        return None

    def _configure(self) -> None:
        with self._lock:
            self.errors = {}
            self.notes = []
            self.detector.set_sensitivity(self.s.sensitivity)
            det, sh = self._resolve()

            # Shutter
            self.shutter = NullShutter()
            try:
                if sh == "arduino":
                    link = self._find_link()
                    if link is None:
                        raise OSError(self.errors.get("shutter") or "no StormTrigger board found")
                    self.shutter = ArduinoShutter(
                        link, hold_ms=self.s.hold_ms, awake=self.s.keep_awake,
                        photodiode=(det == "photodiode" or self.s.photodiode_assist),
                        pd_threshold=self.s.photodiode_threshold, pd_k=self.s.photodiode_k,
                        rearm_ms=self.s.cooldown_ms)
                elif sh == "line":
                    if not self.s.serial_port:
                        raise OSError("choose the USB-serial adapter for the line shutter")
                    self.shutter = LineShutter(self.s.serial_port, line=self.s.line_pin,
                                               focus_line="rts" if self.s.line_pin == "dtr" else "dtr",
                                               hold_ms=self.s.hold_ms, awake=self.s.keep_awake)
                elif sh == "usb":
                    if not self.camera:
                        raise OSError("USB release needs python3-gphoto2")
                    self.shutter = UsbShutter(self.camera)
                self.shutter.open()
            except OSError as e:
                self.errors["shutter"] = str(e)
                self.shutter = NullShutter()

            # Detector
            self.detector_kind, self.detector_name, self.detector_mode = det, "", ""
            try:
                self._start_detector(det)
            except (OSError, ValueError) as e:
                self.errors["detector"] = str(e)
                self.detector_kind = "none"

            if self.s.save_clips and self.detector_kind in ("webcam", "simulated", "liveview"):
                self.clips = ClipRecorder(self.session_dir, self.emit)
        self.emit_state()

    def _start_detector(self, det: str) -> None:
        self.detector.reset()
        self._t_prev_frame = None
        if det == "webcam":
            devs = self._list_video()
            path = self.s.video_device or (devs[0].path if devs else "")
            if not path:
                raise OSError("no webcam found")
            cam = self._video_factory(path).open()
            try:
                modes = cam.modes()
                mode = None
                if self.s.video_mode != "auto":
                    try:
                        mode = Mode.parse(self.s.video_mode)
                    except ValueError:
                        mode = None
                mode = cam.set_mode(mode or choose_mode(modes) or Mode("YUYV", 640, 480, 30))
                self.notes += cam.configure_exposure(self.s.webcam_exposure or None)
                cam.start()
            except Exception:
                cam.close()
                raise
            self.detector_name = cam.card or path
            self.detector_mode = str(mode)
            dev = next((d for d in devs if d.path == path), None)
            if dev is not None and getattr(dev, "internal", False):
                self.notes.append(f"using the built-in camera '{dev.name}': point it at the sky, "
                                  "or plug in a USB webcam and choose it in Setup")
            self._run_source(cam, "webcam")
        elif det == "simulated":
            src = self._sim_factory().open()
            self.detector_name, self.detector_mode = src.name, src.mode
            self._run_source(src, "simulated")
        elif det == "liveview":
            if not self.camera:
                raise OSError("live view needs python3-gphoto2")
            if self.s.mode == "night":
                raise ValueError("night mode needs a webcam or photodiode detector "
                                 "(live view cannot run while the camera exposes)")
            self.detector_name = "D3300 live view"
            self.detector_mode = "JPEG ~640x424"
            self.notes.append("live view is the slowest detector: the mirror must cycle "
                              "before each release")
            if self.shutter.kind in ("arduino", "line"):
                self.notes.append("in live view the D3300 may ignore the remote cable; "
                                  "if releases do not happen, choose USB release")
            self.camera.set_liveview(lambda f: self._process(f, "liveview"))
        elif det == "photodiode":
            link = self._find_link()
            if link is None:
                raise OSError(self.errors.get("shutter") or "no StormTrigger board found")
            self.detector_name = f"photodiode on {self._link_port}"
            self.detector_mode = "~19 kHz ADC, in-board trigger"
            link.configure(self.s.photodiode_threshold, self.s.photodiode_k, self.s.hold_ms,
                           self.s.cooldown_ms)
            link.set_telemetry(True)
        elif det != "none":
            raise ValueError(f"unknown detector {det}")

    def _run_source(self, src, kind: str) -> None:
        self._source = src
        self._src_stop.clear()
        self._src_thread = threading.Thread(target=self._frame_loop, args=(src, kind),
                                            name="detect", daemon=True)
        self._src_thread.start()

    def _teardown(self, keep_link=True) -> None:
        self._src_stop.set()
        if self._src_thread:
            self._src_thread.join(timeout=3)
            self._src_thread = None
        if self._source is not None:
            try:
                self._source.close()
            except Exception:
                pass
            self._source = None
        if self.camera:
            self.camera.set_liveview(None)
        if self.link and self.link.connected:
            self.link.set_telemetry(False)
            self.link.set_auto(False)
        try:
            self.shutter.close()
        except OSError:
            pass
        self.shutter = NullShutter()
        if self.clips:
            self.clips.close()
            self.clips = None
        if not keep_link and self.link:
            self.link.close()
            self.link = None

    def apply(self, settings: Settings) -> None:
        was_armed = self.armed
        self.disarm()
        port_changed = settings.serial_port != self.s.serial_port
        self._teardown(keep_link=not port_changed)
        self.s = settings
        root = paths.output_root(settings.output_dir)
        self.session_dir = paths.session_dir(root)
        os.makedirs(self.session_dir, exist_ok=True)
        if self.camera:
            self.camera.download = settings.download
            self.camera.dest_dir = self.session_dir
            self.camera.fast_trigger = settings.fast_usb_release
            if self.camera.capture_target != settings.capture_target:
                self.camera.capture_target = settings.capture_target
                self.camera.post(self.camera._apply_capture_settings)  # noqa: SLF001
        self._configure()
        if was_armed:
            self.arm()

    # ------------------------------------------------------------- arming ---
    def arm(self) -> bool:
        if self.armed:
            return True
        if self.s.mode == "night":
            exp = self.s.night_exposure_s or (self.camera.exposure_seconds() if self.camera else None)
            try:
                self.night = NightController(self.shutter, exp or 0, self.s.night_gap_s,
                                             self.session_dir, self.emit)
            except ValueError as e:
                self.log("error", f"{e}: set a timed shutter speed on the camera or "
                                  "a night exposure in Settings")
                return False
        self.armed = True
        self.shutter.set_armed(True)
        if isinstance(self.shutter, UsbShutter):
            self.camera.prepare_trigger()
        if self.link and self.link.connected and self.detector_kind == "photodiode":
            # In trigger mode the board fires on its own; in night mode it only
            # reports light levels and the PC finds the flashes.
            self.link.set_auto(self.s.mode != "night")
        rt.gc_armed(True)
        rt.hold_cpu_awake(True)
        if self.night:
            self.night.start()
        self.log("info", "armed" + (" (night mode)" if self.night else ""))
        self.emit_state()
        return True

    def disarm(self) -> None:
        if not self.armed:
            return
        self.armed = False
        if self.night:
            self.night.stop()
            self.night = None
        if self.link and self.link.connected and self.detector_kind == "photodiode":
            self.link.set_auto(False)
        self.shutter.set_armed(False)
        rt.hold_cpu_awake(False)
        rt.gc_armed(False)
        self.log("info", "disarmed")
        self.emit_state()

    # ----------------------------------------------------------- triggers ---
    def _record_trigger(self, t_frame, t_sent, score, source, extra=None) -> int:
        self.trigger_count += 1
        tid = self.trigger_count
        ev = {"type": "trigger", "id": tid, "source": source, "score": round(float(score), 2),
              "t_frame": t_frame, "t_sent": t_sent, "wall": time.time(),
              "reaction_ms": round((t_sent - t_frame) * 1000, 2) if t_frame else None,
              "shutter": self.shutter.kind}
        if extra:
            ev.update(extra)
        self.emit(ev)
        return tid

    def _on_flash(self, t_frame: float, score: float, source: str) -> None:
        """Called on whichever thread saw the flash. Fires first, asks later."""
        if not self.armed:
            return
        if self.night is not None:
            self.night.record_flash(t_frame)
            return
        with self._fire_lock:
            if time.monotonic() - self._last_fire < self.s.cooldown_ms / 1000.0:
                return
            t_sent = self.shutter.fire()
            self._last_fire = t_sent
        tid = self._record_trigger(t_frame, t_sent, score, source)
        if self.clips is not None:
            self.clips.trigger(tid, time.time())

    def test_fire(self) -> None:
        t = self.shutter.fire()
        self._record_trigger(None, t, 0.0, "test")

    def _on_board_fire(self, ev: dict) -> None:
        if ev["src"] != "P":
            return  # echo of a release the PC asked for; already recorded
        t = ev["t"]
        if self.night is not None:
            self.night.record_flash(t)
            return
        hardware = isinstance(self.shutter, ArduinoShutter)
        if not hardware:  # board saw it; the PC releases via USB / line
            with self._fire_lock:
                t = self.shutter.fire()
                self._last_fire = t
        # In-board releases happen in the ADC interrupt (< 0.06 ms after the
        # light rose); the PC only hears about them afterwards.
        self._record_trigger(None if hardware else ev["t"], t, 1.0, "photodiode",
                             {"hardware": hardware, "level": ev["level"], "trip": ev["trip"]})

    def _on_board_telemetry(self, level: int, base: int, trip: int, t: float) -> None:
        if self.detector_kind != "photodiode":
            return
        if trip >= 256:
            score = 0.0
            if not self._pd_saturated:
                self._pd_saturated = True
                self.log("warn", "photodiode saturated: shade the sensor or use a smaller resistor")
        else:
            self._pd_saturated = False
            score = max(0.0, (level - base) / max(1, trip - base))
        if self.night is not None and score >= 1.0 and self.armed:
            self.night.record_flash(t)
        self._level = float(level)
        self._fps = 100.0
        self._tele_add(t, score)

    # ------------------------------------------------------------- frames ---
    def _frame_loop(self, src, kind: str) -> None:
        self.rt_desc = rt.make_thread_realtime(50) if self.s.realtime else "normal priority"
        self.emit_state()
        misses = 0
        while not self._src_stop.is_set():
            try:
                f = src.read(0.5)
            except OSError as e:
                self.errors["detector"] = f"capture failed: {e}"
                self.log("error", f"{kind}: {e}")
                self.emit_state()
                return
            if f is None:
                misses += 1
                if misses == 6:
                    self.log("warn", f"{kind}: no frames for 3 s")
                continue
            misses = 0
            try:
                self._process(f, kind)
            finally:
                src.release(f)

    def _process(self, f, kind: str) -> None:
        t0 = time.monotonic()
        det = self.detector.process(f.luma, f.t)
        if det.flash:
            self._on_flash(f.t, det.score, kind)
        t1 = time.monotonic()
        # Everything below happens after the release has been issued.
        if self.clips is not None:
            self.clips.push(f)
        if self.preview is not None:
            self.preview.offer(f, det.flash)
        if self._t_prev_frame is not None:
            dt = f.t - self._t_prev_frame
            if dt > 0:
                self._fps = 0.9 * self._fps + 0.1 / dt if self._fps else 1.0 / dt
        self._t_prev_frame = f.t
        self._age = 0.9 * self._age + 0.1 * (t0 - f.t)
        self._proc = 0.9 * self._proc + 0.1 * (t1 - t0)
        self._level = det.level
        self._tele_add(f.t, det.score)

    def _tele_add(self, t: float, score: float) -> None:
        self._tele.append((round(t, 4), round(float(score), 3)))
        now = time.monotonic()
        if now - self._tele_last >= TELEMETRY_PERIOD:
            self._tele_last = now
            pts, self._tele = self._tele, []
            self.emit({"type": "telemetry", "points": pts, "fps": round(self._fps, 1),
                       "age_ms": round(self._age * 1000, 2), "proc_ms": round(self._proc * 1000, 3),
                       "level": round(self._level, 1), "armed": self.armed})

    # ----------------------------------------------------------- commands ---
    def handle(self, cmd: dict) -> None:
        c = cmd.get("cmd")
        try:
            if c == "arm":
                self.arm()
            elif c == "disarm":
                self.disarm()
            elif c == "test_fire":
                self.test_fire()
            elif c == "apply":
                self.apply(Settings.from_dict(cmd["settings"]))
            elif c == "devices":
                self.emit(self.devices())
            elif c == "state":
                self.emit_state()
            elif c == "camera_set" and self.camera:
                self.camera.set_config(cmd["key"], cmd["value"])
            elif c == "camera_refresh" and self.camera:
                self.camera.refresh()
            elif c == "release_camera" and self.camera:
                stopped = self.camera.release_holders()
                self.log("info", "stopped " + (", ".join(f"{h['name']} ({h['pid']})" for h in stopped)
                                               or "nothing"))
            elif c == "sensitivity":
                self.s.sensitivity = float(cmd["value"])
                self.detector.set_sensitivity(self.s.sensitivity)
            elif c == "preview" and self.preview:
                self.preview.enabled = bool(cmd.get("enabled", True))
        except Exception as e:  # a bad command must never take the engine down
            self.log("error", f"{c}: {e!r}")
