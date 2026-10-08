"""XDG locations."""
from __future__ import annotations

import os
import re
import time


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "stormwatch")


def pictures_dir() -> str:
    """The user's Pictures folder as configured in user-dirs.dirs (localised
    names on KDE/GNOME), falling back to ~/Pictures."""
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                       "user-dirs.dirs")
    try:
        with open(cfg) as f:
            for line in f:
                m = re.match(r'\s*XDG_PICTURES_DIR="(.*)"', line)
                if m:
                    return os.path.expandvars(m.group(1).replace("$HOME", os.path.expanduser("~")))
    except OSError:
        pass
    return os.path.expanduser("~/Pictures")


def output_root(configured: str = "") -> str:
    return os.path.expanduser(configured) if configured else os.path.join(pictures_dir(), "StormWatch")


def session_dir(root: str) -> str:
    """One folder per night: storms cross midnight, so the day starts at noon."""
    t = time.time() - 12 * 3600
    return os.path.join(root, time.strftime("%Y-%m-%d", time.localtime(t)))
