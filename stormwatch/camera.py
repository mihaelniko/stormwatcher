"""Nikon D3300 over USB through libgphoto2 (python-gphoto2).

One thread owns the gphoto2 Camera; libgphoto2 is not thread-safe, and a
single owner also gives the shutter request a clear priority over everything
else that talks to the camera.

Fast USB release
    libgphoto2's ``trigger_capture()`` on Nikon does four USB transactions
    before the capture command (event check, busy wait in 100 ms steps, event
    check, live-view status) and then blocks until the exposure is written.
    Here the release goes out as ONE PTP transaction - Nikon
    InitiateCaptureRecInMedia (0x9207) with autofocus disabled - through
    libgphoto2's raw "opcode" config widget, and returns as soon as the camera
    accepts it. A busy camera is retried every 2 ms rather than every 100 ms.
    If the body or the libgphoto2 build does not take that path, the standard
    ``trigger_capture()`` (with autofocus set to Off) is used instead.

Downloads
    New files are found by polling camera events with a zero timeout (one USB
    transaction) and read in 1 MB chunks; a shutter request is served between
    chunks, so a download never delays a release by more than one chunk.
"""
from __future__ import annotations

import glob
import os
import queue
import threading
import time
from collections import deque

from .pixfmt import decode_jpeg_luma
from .v4l2 import Frame

NIKON_VID = "04b0"
CHUNK = 1 << 20

# libgphoto2 error codes (gphoto2-result.h / gphoto2-port-result.h)
GP_ERROR = -1
GP_ERROR_BAD_PARAMETERS = -2
GP_ERROR_NOT_SUPPORTED = -6
GP_ERROR_IO = -7
GP_ERROR_TIMEOUT = -10
GP_ERROR_IO_USB_FIND = -52
GP_ERROR_IO_USB_CLAIM = -53
GP_ERROR_MODEL_NOT_FOUND = -105
GP_ERROR_CAMERA_BUSY = -110
DISCONNECT_ERRORS = {GP_ERROR_IO, -8, -11, -12, -13, -14, -15, -16, -17, -20, -21, -22, -23,
                     -24, -25, -26, -27, -28, -29, -30, -31, -32, -33, -34, -35, -36, -37,
                     -38, -39, -40, -41, -42, -43, -44, -45, -46, -47, -48, -49, -50,
                     -51, GP_ERROR_IO_USB_FIND, GP_ERROR_IO_USB_CLAIM, -54, -55, -56, -57,
                     -58, -59, -60, GP_ERROR_MODEL_NOT_FOUND}

OP_NIKON_CHANGE_CAMERA_MODE = 0x90C2
OP_NIKON_CAPTURE_IN_MEDIA = 0x9207
NO_AF = 0xFFFFFFFF

# Settings shown in the UI (libgphoto2 names for Nikon bodies).
CONFIG_KEYS = ["shutterspeed", "f-number", "iso", "imagequality", "capturemode",
               "whitebalance", "exposurecompensation"]
STATUS_KEYS = ["batterylevel", "expprogram", "focusmode", "cameramodel", "lensname",
               "flashopen", "availableshots"]

# libgphoto2 builds these two radio widgets from fixed tables, in this order.
# Choosing by index keeps us independent of the UI language.
CAPTURETARGET_CHOICES = ("ram", "card")   # "Internal RAM", "Memory card"
AUTOFOCUS_CHOICES = ("on", "off")


def usb_device_node(port: str) -> str | None:
    """'usb:001,005' -> '/dev/bus/usb/001/005'."""
    if port and port.startswith("usb:") and "," in port:
        bus, dev = port[4:].split(",", 1)
        return f"/dev/bus/usb/{bus.zfill(3)}/{dev.zfill(3)}"
    return None


def nikon_usb_nodes() -> list[str]:
    """USB device nodes of every Nikon attached, whether or not gphoto2 sees it."""
    out = []
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                if f.read().strip() != NIKON_VID:
                    continue
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(d, "devnum")) as f:
                dev = int(f.read())
            out.append(f"/dev/bus/usb/{bus:03d}/{dev:03d}")
        except (OSError, ValueError):
            continue
    return out


def find_holders(nodes) -> list[dict]:
    """Processes (of this user) that have one of the USB device nodes open -
    usually gvfsd-gphoto2 or a KDE camera KIO worker grabbing the D3300."""
    nodes = set(nodes)
    out = []
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = int(fd_dir.split("/")[2])
        if pid == os.getpid():
            continue
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(os.path.join(fd_dir, fd)) in nodes:
                        with open(f"/proc/{pid}/comm") as f:
                            out.append({"pid": pid, "name": f.read().strip()})
                        raise StopIteration
                except OSError:
                    continue
        except StopIteration:
            continue
        except OSError:
            continue
    return out


def _parse_seconds(text: str) -> float | None:
    """'1/250' -> 0.004, '4' -> 4.0, '2.5s' -> 2.5, 'Bulb' -> None."""
    t = (text or "").strip().lower().rstrip("s").strip()
    try:
        if "/" in t:
            a, b = t.split("/", 1)
            return float(a) / float(b)
        return float(t)
    except ValueError:
        return None


class CameraWorker(threading.Thread):
    def __init__(self, emit, *, gp=None, download="jpeg", dest_dir=".", capture_target="card",
                 fast_trigger=True, decode_width=192):
        super().__init__(name="camera", daemon=True)
        if gp is None:
            import gphoto2 as gp  # noqa: N813
        self.gp = gp
        self.emit = emit
        self.download = download            # "all" | "jpeg" | "none"
        self.dest_dir = dest_dir
        self.capture_target = capture_target  # "card" | "ram"
        self.fast_trigger = fast_trigger
        self.decode_width = decode_width
        self.on_file = None                  # callback(path, stem, t_added): downloaded
        self.on_file_added = None            # callback(stem, t): a new picture is on the card
        self.on_trigger_done = None          # callback(info dict): release confirmed

        self._cam = None
        self._port = ""
        self.model = ""
        self.state = "searching"
        self.info: dict = {}
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._trigger_pending = 0
        self._trigger_t = 0.0
        self._cmds: queue.SimpleQueue = queue.SimpleQueue()
        self._downloads: deque = deque()
        self._liveview_cb = None
        self._lv_active = False
        self._next_poll = 0.0
        self._fast_poll_until = 0.0
        self._next_status = 0.0
        self._opcode_widget = None
        self._direct_ok = None               # None = untested, True/False after first use
        self._capture_args = (NO_AF, 0)
        self._prepared = False
        self._last_blocked: list = []
        self.stats = {"triggers": 0, "downloads": 0}

    # ------------------------------------------------------------------ API --
    def request_trigger(self) -> float:
        """Thread-safe and non-blocking: ask for a release now."""
        t = time.monotonic()
        self._trigger_t = t
        self._trigger_pending += 1
        self._wake.set()
        return t

    def call(self, fn, *args, timeout=10.0):
        """Run ``fn(*args)`` on the camera thread and return its result."""
        done = threading.Event()
        box = {}

        def job():
            try:
                box["value"] = fn(*args)
            except Exception as e:  # handed back to the caller
                box["error"] = e
            done.set()

        self._cmds.put(job)
        self._wake.set()
        if not done.wait(timeout):
            raise TimeoutError("camera did not answer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def post(self, fn, *args) -> None:
        self._cmds.put(lambda: fn(*args))
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        self.join(timeout=5.0)

    def set_liveview(self, callback) -> None:
        """Stream live-view frames to ``callback(frame)`` (None to stop)."""
        self.post(self._set_liveview, callback)

    def prepare_trigger(self) -> None:
        self.post(self._prepare_trigger)

    def set_config(self, key, value) -> None:
        self.post(self._set_config_and_report, key, value)

    def refresh(self) -> None:
        self.post(self._report)

    @property
    def connected(self) -> bool:
        return self._cam is not None

    # ----------------------------------------------------------- thread ------
    def run(self) -> None:
        self._set_state("searching")
        while not self._halt.is_set():
            if self._cam is None:
                self._run_commands(offline=True)
                if not self._connect():
                    self._wake.wait(2.0)
                    self._wake.clear()
                continue
            try:
                self._service()
            except self.gp.GPhoto2Error as e:
                self._on_error(e)
            except Exception as e:  # never let the camera thread die
                self.emit({"type": "log", "level": "error", "msg": f"camera: {e!r}"})
                time.sleep(0.2)
        self._disconnect(quiet=True)

    def _service(self) -> None:
        self._wake.clear()
        if self._trigger_pending:
            self._do_trigger()
        self._run_commands()
        if self._liveview_cb is not None:
            self._liveview_step()
            if time.monotonic() >= self._next_poll:
                self._poll_events()
                self._next_poll = time.monotonic() + 0.5
            return
        now = time.monotonic()
        if now >= self._next_poll:
            self._poll_events()
            fast = now < self._fast_poll_until
            self._next_poll = time.monotonic() + (0.05 if fast else 0.3)
        if self._downloads:
            self._download_next()
            return
        if now >= self._next_status:
            self._next_status = now + 30.0
            self._report(quick=True)
        self._wake.wait(max(0.0, min(self._next_poll - time.monotonic(), 0.3)))

    def _run_commands(self, offline=False) -> None:
        while True:
            try:
                job = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                job()
            except self.gp.GPhoto2Error as e:
                if offline:
                    continue
                self._on_error(e)
                if self._cam is None:
                    return
            except Exception as e:
                self.emit({"type": "log", "level": "error", "msg": f"camera command: {e!r}"})

    # ------------------------------------------------------- connection ------
    def _set_state(self, state, **extra) -> None:
        self.state = state
        self.emit({"type": "camera", "state": state, "model": self.model, **extra})

    def _autodetect(self):
        gp = self.gp
        try:
            cl = gp.check_result(gp.gp_camera_autodetect())
            found = [(cl.get_name(i), cl.get_value(i)) for i in range(cl.count())]
        except gp.GPhoto2Error:
            return None, None
        found.sort(key=lambda np: "nikon" not in np[0].lower())
        return found[0] if found else (None, None)

    def _connect(self) -> bool:
        gp = self.gp
        name, port = self._autodetect()
        if not name:
            nodes = nikon_usb_nodes()
            if nodes:
                if not all(os.access(n, os.R_OK | os.W_OK) for n in nodes):
                    if self.state != "no-permission":
                        self._set_state("no-permission", error=(
                            "The Nikon is connected but this user may not open "
                            f"{nodes[0]}. Run packaging/install.sh (udev rules) and replug."))
                else:
                    self._report_blocked(nodes, "Nikon on USB but libgphoto2 cannot open it")
            elif self.state != "searching":
                self._set_state("searching")
            return False
        cam = gp.Camera()
        try:
            pil = gp.PortInfoList()
            pil.load()
            cam.set_port_info(pil[pil.lookup_path(port)])
            al = gp.CameraAbilitiesList()
            al.load()
            cam.set_abilities(al[al.lookup_model(name)])
            cam.init()
        except gp.GPhoto2Error as e:
            node = usb_device_node(port)
            if e.code == GP_ERROR_IO_USB_CLAIM and node:
                self._report_blocked([node], "another program holds the camera")
            elif self.state != "error":
                self._set_state("error", error=str(e))
            return False
        self._cam, self._port, self.model = cam, port, name
        self._last_blocked = []
        self._prepared = False
        self._direct_ok = None
        self._opcode_widget = None
        self._apply_capture_settings()
        self._set_state("ready", port=port)
        self._report()
        self._next_poll = 0.0
        return True

    def _report_blocked(self, nodes, why) -> None:
        holders = find_holders(nodes)
        if holders != self._last_blocked or self.state != "blocked":
            self._last_blocked = holders
            self._set_state("blocked", error=why, holders=holders)

    def release_holders(self) -> list:
        """Stop desktop services that grabbed the camera (gvfs, KIO)."""
        import signal

        stopped = []
        for h in find_holders(nikon_usb_nodes()):
            try:
                os.kill(h["pid"], signal.SIGTERM)
                stopped.append(h)
            except OSError:
                pass
        self._wake.set()
        return stopped

    def _disconnect(self, quiet=False) -> None:
        if self._cam is not None:
            try:
                self._cam.exit()
            except Exception:
                pass
        self._cam = None
        self._lv_active = False
        self._downloads.clear()
        if not quiet:
            self._set_state("searching")

    def _on_error(self, e) -> None:
        code = getattr(e, "code", GP_ERROR)
        if code in DISCONNECT_ERRORS:
            self.emit({"type": "log", "level": "warn", "msg": f"camera disconnected: {e}"})
            self._disconnect()
        else:
            self.emit({"type": "log", "level": "warn", "msg": f"camera: {e}"})

    # ----------------------------------------------------------- config ------
    def _widget(self, key):
        cam = self._cam
        if hasattr(cam, "get_single_config"):
            try:
                return cam.get_single_config(key), None
            except self.gp.GPhoto2Error:
                pass
        tree = cam.get_config()
        return tree.get_child_by_name(key), tree

    def _put(self, key, widget, tree) -> None:
        if tree is None and hasattr(self._cam, "set_single_config"):
            self._cam.set_single_config(key, widget)
        else:
            self._cam.set_config(tree)

    def _choice_by_index(self, key, index) -> bool:
        try:
            w, tree = self._widget(key)
            if w.count_choices() > index:
                w.set_value(w.get_choice(index))
                self._put(key, w, tree)
                return True
        except self.gp.GPhoto2Error:
            pass
        return False

    def _apply_capture_settings(self) -> None:
        # Release without autofocus (the standard path reads this), and save
        # to the card so the camera never waits for the PC. Re-applied on
        # every connect, so there is nothing to do while unplugged.
        if self._cam is None:
            return
        self._choice_by_index("autofocus", AUTOFOCUS_CHOICES.index("off"))
        target = "card" if self.capture_target == "card" else "ram"
        self._choice_by_index("capturetarget", CAPTURETARGET_CHOICES.index(target))
        self._capture_args = (NO_AF, 0 if target == "card" else 1)

    def _describe(self, key) -> dict | None:
        gp = self.gp
        try:
            w, _ = self._widget(key)
        except self.gp.GPhoto2Error:
            return None
        if w is None:
            return None
        try:
            t = w.get_type()
            d = {"key": key, "label": w.get_label(), "value": w.get_value(),
                 "readonly": bool(w.get_readonly())}
            if t in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                d["choices"] = [w.get_choice(i) for i in range(w.count_choices())]
            return d
        except self.gp.GPhoto2Error:
            return None

    def snapshot(self) -> dict:
        settings = {k: d for k in CONFIG_KEYS if (d := self._describe(k))}
        status = {k: d["value"] for k in STATUS_KEYS if (d := self._describe(k))}
        return {"settings": settings, "status": status}

    def _report(self, quick=False) -> None:
        if self._cam is None:
            return
        if quick:
            st = {k: d["value"] for k in ("batterylevel", "expprogram", "focusmode")
                  if (d := self._describe(k))}
            self.info.setdefault("status", {}).update(st)
        else:
            self.info = self.snapshot()
        self.emit({"type": "camera", "state": self.state, "model": self.model,
                   "port": self._port, **self.info, "warnings": self.warnings()})

    def warnings(self) -> list[str]:
        st = self.info.get("status", {})
        out = []
        fm = str(st.get("focusmode", "")).lower()
        if fm and "manual" not in fm and fm not in ("mf", "m"):
            out.append("Lens is in autofocus: set the lens switch to M and focus at infinity "
                       "for the fastest release.")
        prog = str(st.get("expprogram", ""))
        if prog and prog.upper() != "M":
            out.append(f"Mode dial is on {prog}: use M so the camera never waits to meter.")
        return out

    def _set_config_and_report(self, key, value) -> None:
        if self._cam is None:
            self.emit({"type": "log", "level": "warn", "msg": "camera not connected"})
            return
        w, tree = self._widget(key)
        w.set_value(value)
        self._put(key, w, tree)
        self._report()

    def exposure_seconds(self) -> float | None:
        d = self.info.get("settings", {}).get("shutterspeed")
        return _parse_seconds(d["value"]) if d else None

    # ---------------------------------------------------------- shutter ------
    def _opcode(self, op, *params) -> None:
        if self._opcode_widget is None:
            self._opcode_widget, _ = self._widget("opcode")
        text = ",".join(["0x%04x" % op] + ["0x%x" % p for p in params])
        self._opcode_widget.set_value(text)
        self._cam.set_single_config("opcode", self._opcode_widget)

    def _prepare_trigger(self) -> None:
        """Put the body into PC-control mode once, before the first release."""
        if self._prepared or self._cam is None:
            return
        self._prepared = True
        if self.fast_trigger and hasattr(self._cam, "set_single_config"):
            try:
                self._opcode(OP_NIKON_CHANGE_CAMERA_MODE, 1)
            except self.gp.GPhoto2Error:
                pass  # older bodies do not have it; the release works anyway

    def _direct_release(self) -> str:
        """One PTP transaction. Returns "ok", "busy" (camera still exposing or
        writing after 1.5 s of retries) or "unsupported"."""
        deadline = time.monotonic() + 1.5
        args = self._capture_args
        while True:
            try:
                self._opcode(OP_NIKON_CAPTURE_IN_MEDIA, *args)
                return "ok"
            except self.gp.GPhoto2Error as e:
                if e.code == GP_ERROR_BAD_PARAMETERS and len(args) == 2:
                    args = (NO_AF,)  # bodies that take the one-parameter form
                    self._capture_args = args
                    continue
                if e.code in DISCONNECT_ERRORS:
                    raise
                if e.code in (GP_ERROR_CAMERA_BUSY, GP_ERROR):
                    if time.monotonic() < deadline:
                        time.sleep(0.002)  # busy writing the previous frame
                        continue
                    return "busy"
                return "unsupported"

    def _do_trigger(self) -> None:
        n = self._trigger_pending
        self._trigger_pending = 0
        t_req = self._trigger_t
        if not self._prepared:
            self._prepare_trigger()
        t0 = time.monotonic()
        method = "direct"
        ok = False
        if self.fast_trigger and self._direct_ok is not False and hasattr(self._cam, "set_single_config"):
            res = self._direct_release()
            ok = res == "ok"
            if ok:
                self._direct_ok = True
            elif res == "unsupported":
                self._direct_ok = False
                self.emit({"type": "log", "level": "info",
                           "msg": "fast PTP release not available on this body; using libgphoto2's"})
        if not ok:
            method = "libgphoto2"
            self._cam.trigger_capture()
        t1 = time.monotonic()
        self.stats["triggers"] += 1
        self._fast_poll_until = t1 + 8.0
        self._next_poll = 0.0
        # When the shutter actually went: the direct release is accepted the
        # moment the camera fires; libgphoto2's path returns only after the
        # exposure, so its start is the best estimate there.
        info = {"type": "camera_fired", "t_request": t_req, "t_sent": t0, "t_accepted": t1,
                "t_released": t1 if method == "direct" else t0,
                "method": method, "coalesced": n - 1}
        self.emit(info)
        if self.on_trigger_done:
            self.on_trigger_done(info)

    # ----------------------------------------------------------- events ------
    def _poll_events(self) -> None:
        gp = self.gp
        for _ in range(16):
            ev, data = self._cam.wait_for_event(0)
            if ev == gp.GP_EVENT_TIMEOUT:
                return
            if ev == gp.GP_EVENT_FILE_ADDED:
                self._on_file_added(data.folder, data.name)

    def _wanted(self, name: str) -> bool:
        if self.download == "none":
            return False
        if self.download == "jpeg":
            return name.lower().endswith((".jpg", ".jpeg"))
        return True

    def _on_file_added(self, folder, name) -> None:
        t = time.monotonic()
        stem = os.path.splitext(name)[0]
        self.emit({"type": "camera_file", "folder": folder, "name": name})
        if self.on_file_added:
            self.on_file_added(stem, t)
        if self._wanted(name):
            self._downloads.append((folder, name, t, stem))
        elif self.on_file:
            self.on_file(None, stem, t)

    def _download_next(self) -> None:
        gp = self.gp
        folder, name, t_added, stem = self._downloads.popleft()
        os.makedirs(self.dest_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        path = os.path.join(self.dest_dir, f"{stamp}_{name}")
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.dest_dir, f"{stamp}_{n}_{name}")
        tmp = path + ".part"
        try:
            data = self._read_chunked(folder, name)
        except self.gp.GPhoto2Error as e:
            if e.code not in (GP_ERROR_NOT_SUPPORTED, GP_ERROR_BAD_PARAMETERS):
                raise
            data = bytes(memoryview(self._cam.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL)
                                    .get_data_and_size()))
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        if self.capture_target == "ram":
            try:
                self._cam.file_delete(folder, name)
            except self.gp.GPhoto2Error:
                pass
        self.stats["downloads"] += 1
        self.emit({"type": "file", "path": path, "name": name,
                   "seconds": round(time.monotonic() - t_added, 3)})
        if self.on_file:
            self.on_file(path, stem, t_added)

    def _read_chunked(self, folder, name) -> bytearray:
        gp = self.gp
        size = self._cam.file_get_info(folder, name).file.size
        buf = bytearray(size)
        view = memoryview(buf)
        off = 0
        while off < size:
            if self._trigger_pending:      # a release never waits for a download
                self._do_trigger()
            n = self._cam.file_read(folder, name, gp.GP_FILE_TYPE_NORMAL, off,
                                    view[off:off + CHUNK])
            if n <= 0:
                break
            off += n
        return buf

    # -------------------------------------------------------- live view ------
    def _set_liveview(self, callback) -> None:
        self._liveview_cb = callback
        if callback is None and self._lv_active and self._cam is not None:
            self._lv_active = False
            try:  # leave live view so the mirror drops and releases are fast again
                w, tree = self._widget("viewfinder")
                w.set_value(0)
                self._put("viewfinder", w, tree)
            except (self.gp.GPhoto2Error, AttributeError, TypeError):
                pass

    def _liveview_step(self) -> None:
        cf = self._cam.capture_preview()
        t = time.monotonic()
        self._lv_active = True
        data = memoryview(cf.get_data_and_size())
        try:
            luma = decode_jpeg_luma(data, self.decode_width)
        except Exception:
            return
        jpeg = bytes(data)
        frame = Frame(t=t, seq=0, luma=luma, raw=jpeg, fmt="JPEG",
                      width=luma.shape[1], height=luma.shape[0])
        cb = self._liveview_cb
        if cb is not None:
            cb(frame)
        if self._trigger_pending:
            self._do_trigger()
