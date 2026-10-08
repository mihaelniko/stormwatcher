"""User settings, stored as JSON in ~/.config/stormwatch/settings.json so the
UI and headless mode share them."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from .paths import config_dir


@dataclass
class Settings:
    # Detection ---------------------------------------------------------------
    detector: str = "auto"          # auto | webcam | photodiode | liveview | simulated | none
    video_device: str = ""          # /dev/videoN or /dev/v4l/by-id/...; "" = first one
    video_mode: str = "auto"        # "auto" or e.g. "YUYV 640x480@30"
    webcam_exposure: int = 0        # 0 = auto (frame rate held constant); >0 = manual, 100 us units
    sensitivity: float = 0.7        # 0..1
    # Shutter -----------------------------------------------------------------
    shutter: str = "auto"           # auto | arduino | line | usb | none
    serial_port: str = ""           # StormTrigger board or USB-UART; "" = auto
    line_pin: str = "dtr"           # line shutter: dtr or rts (focus uses the other)
    hold_ms: int = 150              # how long the remote "button" is held
    keep_awake: bool = True         # hold half-press while armed (faster release)
    cooldown_ms: int = 500          # minimum time between releases
    photodiode_threshold: int = 12  # minimum rise, ADC counts (0-255)
    photodiode_k: int = 6           # rise must also exceed k x sensor noise
    photodiode_assist: bool = False # with the Arduino shutter: photodiode fires too
    fast_usb_release: bool = True   # one-transaction PTP release (falls back automatically)
    # Mode --------------------------------------------------------------------
    mode: str = "trigger"           # trigger | night
    night_exposure_s: float = 0.0   # 0 = read the shutter speed from the camera
    night_gap_s: float = 0.6        # pause between night exposures
    # Camera / output ---------------------------------------------------------
    download: str = "jpeg"          # all | jpeg | none
    capture_target: str = "card"    # card | ram
    output_dir: str = ""            # "" = ~/Pictures/StormWatch
    save_clips: bool = True         # keep the detector frames around each trigger
    realtime: bool = True           # real-time scheduling for the detection thread

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for f in fields(cls):
            if f.name in d:
                v = d[f.name]
                try:
                    if f.type in ("bool", bool):
                        v = bool(v)
                    elif f.type in ("int", int):
                        v = int(v)
                    elif f.type in ("float", float):
                        v = float(v)
                    else:
                        v = str(v)
                except (TypeError, ValueError):
                    continue
                setattr(s, f.name, v)
        return s

    def to_dict(self) -> dict:
        return asdict(self)


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load(path: str | None = None) -> Settings:
    path = path or settings_path()
    try:
        with open(path) as f:
            return Settings.from_dict(json.load(f))
    except (OSError, ValueError):
        return Settings()


def save(s: Settings, path: str | None = None) -> None:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s.to_dict(), f, indent=2)
    os.replace(tmp, path)
