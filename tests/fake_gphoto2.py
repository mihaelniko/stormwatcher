"""A stand-in for python-gphoto2 that behaves like a D3300 on USB.

Only the calls stormwatch.camera makes are implemented. A single ``Device``
object holds the simulated camera so a test can unplug/replug, make it busy,
or refuse the fast release path.
"""
from __future__ import annotations

import io
import threading
import time

GP_EVENT_UNKNOWN, GP_EVENT_TIMEOUT, GP_EVENT_FILE_ADDED = 0, 1, 2
GP_FILE_TYPE_NORMAL = 1
GP_WIDGET_WINDOW, GP_WIDGET_SECTION, GP_WIDGET_TEXT, GP_WIDGET_RANGE = 0, 1, 2, 3
GP_WIDGET_TOGGLE, GP_WIDGET_RADIO, GP_WIDGET_MENU = 4, 5, 6


class GPhoto2Error(Exception):
    def __init__(self, code):
        super().__init__(f"gphoto2 error {code}")
        self.code = code


def check_result(r):
    if isinstance(r, (list, tuple)) and r and isinstance(r[0], int):
        if r[0] < 0:
            raise GPhoto2Error(r[0])
        return r[1] if len(r) == 2 else r[1:]
    return r


class Device:
    def __init__(self):
        self.present = True
        self.claim_error = False
        self.settings = {
            "shutterspeed": ["1/125", ["1/4000", "1/250", "1/125", "1/30", "1/4", "1", "4", "Bulb"]],
            "f-number": ["f/8", ["f/3.5", "f/5.6", "f/8", "f/11"]],
            "iso": ["400", ["100", "200", "400", "800", "1600"]],
            "imagequality": ["JPEG Fine", ["JPEG Basic", "JPEG Fine", "NEF (Raw)", "NEF+Fine"]],
            "capturemode": ["Single Shot", ["Single Shot", "Burst"]],
            "capturetarget": ["Internal RAM", ["Internal RAM", "Memory card"]],
            "autofocus": ["On", ["On", "Off"]],
            "batterylevel": ["80%", None],
            "expprogram": ["M", None],
            "focusmode": ["Manual", None],
            "cameramodel": ["D3300", None],
        }
        self.opcodes = []          # raw PTP commands received
        self.std_triggers = 0
        self.direct_supported = True
        self.one_param_only = False
        self.busy_until = 0.0
        self.busy_responses = 0    # force N busy answers
        self.exposure = 0.05
        self.events = []           # (deliver_at, folder, name)
        self.files = {}            # (folder, name) -> bytes
        self.counter = 0
        self.raw_plus_jpeg = False
        self.read_delay = 0.0      # per file_read chunk
        self.log = []              # ordered operations
        self.io_error_next = False
        self.lock = threading.Lock()

    def shoot(self):
        self.counter += 1
        now = time.monotonic()
        self.busy_until = now + self.exposure
        names = [f"DSC_{self.counter:04d}.JPG"]
        if self.raw_plus_jpeg:
            names.insert(0, f"DSC_{self.counter:04d}.NEF")
        for n in names:
            data = (n.encode() * 250_000)[: 2_500_000]  # ~2.5 MB: three 1 MB chunks
            self.files[("/store_00010001/DCIM/100D3300", n)] = data
            self.events.append((now + self.exposure + 0.01, "/store_00010001/DCIM/100D3300", n))


DEVICE = Device()


class _List:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def get_name(self, i):
        return self.items[i][0]

    def get_value(self, i):
        return self.items[i][1]


def gp_camera_autodetect(context=None):
    if not DEVICE.present:
        return [0, _List([])]
    return [0, _List([("Nikon DSC D3300", "usb:001,005")])]


class PortInfoList:
    def load(self):
        pass

    def lookup_path(self, p):
        return 0

    def __getitem__(self, i):
        return "port-info"


class CameraAbilitiesList(PortInfoList):
    def lookup_model(self, m):
        return 0


class Widget:
    def __init__(self, name, value, choices, wtype=None):
        self.name, self.value, self.choices = name, value, choices
        self.type = wtype if wtype is not None else (GP_WIDGET_RADIO if choices else GP_WIDGET_TEXT)

    def get_type(self):
        return self.type

    def get_name(self):
        return self.name

    def get_label(self):
        return self.name.title()

    def get_value(self):
        return self.value

    def set_value(self, v):
        if self.choices and v not in self.choices:
            raise GPhoto2Error(-2)
        self.value = v

    def get_readonly(self):
        return 0 if self.choices else 1

    def count_choices(self):
        return len(self.choices or [])

    def get_choice(self, i):
        return self.choices[i]


class _Info:
    class file:  # noqa: N801
        size = 0


class CameraFile:
    def __init__(self, data):
        self.data = data

    def get_data_and_size(self):
        return memoryview(self.data)


class FilePath:
    def __init__(self, folder, name):
        self.folder, self.name = folder, name


def _jpeg(level=40):
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (640, 424), (level, level, level)).save(b, "JPEG")
    return b.getvalue()


class Camera:
    def __init__(self):
        self.inited = False

    def _check(self):
        d = DEVICE
        if not d.present:
            raise GPhoto2Error(-52)
        if d.io_error_next:
            d.io_error_next = False
            d.present = False
            raise GPhoto2Error(-7)

    def set_port_info(self, p):
        pass

    def set_abilities(self, a):
        pass

    def init(self):
        if DEVICE.claim_error:
            raise GPhoto2Error(-53)
        self._check()
        self.inited = True

    def exit(self):
        self.inited = False

    def get_single_config(self, key):
        self._check()
        if key == "opcode":
            return Widget("opcode", "0x1001,0xparam1,0xparam2", None, GP_WIDGET_TEXT)
        if key == "viewfinder":
            return Widget("viewfinder", 1, None, GP_WIDGET_TOGGLE)
        if key not in DEVICE.settings:
            raise GPhoto2Error(-2)
        v, ch = DEVICE.settings[key]
        return Widget(key, v, ch)

    def set_single_config(self, key, w):
        self._check()
        d = DEVICE
        if key == "opcode":
            parts = [int(x, 16) for x in w.value.split(",")]
            d.log.append(("opcode", tuple(parts)))
            if not d.direct_supported:
                raise GPhoto2Error(-6)
            if parts[0] == 0x9207:
                if d.one_param_only and len(parts) == 3:
                    raise GPhoto2Error(-2)
                if d.busy_responses > 0:
                    d.busy_responses -= 1
                    raise GPhoto2Error(-110)
                if time.monotonic() < d.busy_until:
                    raise GPhoto2Error(-110)
                d.opcodes.append(tuple(parts))
                d.shoot()
            else:
                d.opcodes.append(tuple(parts))
            return
        if key == "viewfinder":
            d.log.append(("viewfinder", w.value))
            return
        d.settings[key][0] = w.value

    def get_config(self):
        raise GPhoto2Error(-6)

    def trigger_capture(self):
        self._check()
        d = DEVICE
        d.log.append(("trigger_capture",))
        d.std_triggers += 1
        while time.monotonic() < d.busy_until:
            time.sleep(0.01)
        d.shoot()
        time.sleep(d.exposure)  # libgphoto2 waits for the exposure to finish

    def wait_for_event(self, timeout):
        self._check()
        d = DEVICE
        now = time.monotonic()
        for i, (at, folder, name) in enumerate(d.events):
            if at <= now:
                d.events.pop(i)
                return GP_EVENT_FILE_ADDED, FilePath(folder, name)
        return GP_EVENT_TIMEOUT, None

    def file_get_info(self, folder, name):
        self._check()
        info = _Info()
        info.file = type("F", (), {"size": len(DEVICE.files[(folder, name)])})()
        return info

    def file_read(self, folder, name, ftype, offset, buf):
        self._check()
        d = DEVICE
        data = d.files[(folder, name)]
        n = min(len(buf), len(data) - offset)
        buf[:n] = data[offset:offset + n]
        d.log.append(("read", name, offset))
        if d.read_delay:
            time.sleep(d.read_delay)
        return n

    def file_get(self, folder, name, ftype):
        return CameraFile(DEVICE.files[(folder, name)])

    def file_delete(self, folder, name):
        DEVICE.files.pop((folder, name), None)

    def capture_preview(self):
        self._check()
        time.sleep(0.02)
        DEVICE.log.append(("preview",))
        return CameraFile(_jpeg(getattr(DEVICE, "lv_level", 40)))
