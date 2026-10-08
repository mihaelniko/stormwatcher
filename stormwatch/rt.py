"""Real-time scheduling for the detection thread, without root.

1. SCHED_FIFO directly, if RLIMIT_RTPRIO allows it (install.sh --realtime
   adds a limits.d entry for that).
2. Otherwise ask RealtimeKit (rtkit-daemon, which PipeWire uses on every
   Fedora desktop) over D-Bus for SCHED_RR. rtkit requires RLIMIT_RTTIME to be
   set; a SIGXCPU handler demotes the thread before the hard limit is hit.
3. Otherwise stay at normal priority.

Also: parent-death signal for the engine process, keeping the CPU out of deep
idle states while armed, and holding the cyclic GC off while armed.
"""
from __future__ import annotations

import ctypes
import gc
import os
import resource
import signal
import struct
import subprocess
import threading

_rt_threads: set[int] = set()
_handler_installed = False
_dma_fd = None
_libc = None


def _lib():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def set_parent_death_signal(sig=signal.SIGTERM) -> None:
    """Die with the parent (the UI) even if it crashes."""
    try:
        _lib().prctl(1, int(sig), 0, 0, 0)  # PR_SET_PDEATHSIG
    except (AttributeError, OSError):
        pass


def _rtkit_property(name: str) -> int | None:
    try:
        out = subprocess.run(
            ["busctl", "--system", "get-property", "org.freedesktop.RealtimeKit1",
             "/org/freedesktop/RealtimeKit1", "org.freedesktop.RealtimeKit1", name],
            capture_output=True, text=True, timeout=2).stdout.split()
        return int(out[1]) if len(out) >= 2 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _on_sigxcpu(signum, frame):
    # Close to the RLIMIT_RTTIME hard limit: drop to normal scheduling.
    for tid in list(_rt_threads):
        try:
            os.sched_setscheduler(tid, os.SCHED_OTHER, os.sched_param(0))
        except OSError:
            pass
    _rt_threads.clear()


def make_thread_realtime(priority: int = 50) -> str:
    """Call from the thread to promote. Returns a short description."""
    tid = threading.get_native_id()
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(priority))
        return f"SCHED_FIFO {priority}"
    except (PermissionError, OSError, AttributeError):
        pass
    # rtkit needs RLIMIT_RTTIME; SIGXCPU at the soft limit would kill the
    # process by default, so only go this way once our handler is installed.
    max_prio = _rtkit_property("MaxRealtimePriority") if _handler_installed else None
    max_rttime = _rtkit_property("RTTimeUSecMax") if max_prio else None
    if max_prio and max_rttime:
        prio = min(priority, max_prio)
        try:
            resource.setrlimit(resource.RLIMIT_RTTIME, (int(max_rttime * 0.75), max_rttime))
            r = subprocess.run(
                ["busctl", "--system", "call", "org.freedesktop.RealtimeKit1",
                 "/org/freedesktop/RealtimeKit1", "org.freedesktop.RealtimeKit1",
                 "MakeThreadRealtime", "tu", str(tid), str(prio)],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                _rt_threads.add(tid)
                return f"rtkit SCHED_RR {prio}"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        os.setpriority(os.PRIO_PROCESS, tid, -10)
        return "nice -10"
    except OSError:
        return "normal priority"


def install_handlers() -> None:
    """Call from the main thread of the engine process."""
    global _handler_installed
    try:
        signal.signal(signal.SIGXCPU, _on_sigxcpu)
        _handler_installed = True
    except (ValueError, OSError):
        pass


def hold_cpu_awake(on: bool) -> bool:
    """Ask the kernel to keep CPUs out of deep C-states (PM QoS). Only works if
    /dev/cpu_dma_latency is writable (install.sh --realtime)."""
    global _dma_fd
    if on and _dma_fd is None:
        try:
            _dma_fd = os.open("/dev/cpu_dma_latency", os.O_WRONLY | os.O_CLOEXEC)
            os.write(_dma_fd, struct.pack("i", 0))
            return True
        except OSError:
            _dma_fd = None
            return False
    if not on and _dma_fd is not None:
        os.close(_dma_fd)
        _dma_fd = None
    return _dma_fd is not None


def gc_armed(armed: bool) -> None:
    """No cyclic-GC pauses while armed; collect when disarmed."""
    if armed:
        gc.collect()
        gc.freeze()
        gc.disable()
    else:
        gc.enable()
        gc.unfreeze()
        gc.collect()
