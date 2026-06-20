"""
StormWatcher - Nikon lightning trigger (Windows / digiCamControl)
=================================================================

Watches the Nikon's live-view feed for a lightning flash and fires the
shutter as fast as the camera + USB allow.

It talks to digiCamControl's built-in web server (default
http://localhost:5513), so the camera session stays "warm" - shots fire far
faster than spawning the command-line tool once per photo.

>> Read the README before running. Lightning is faster than any
>> detect-then-shoot loop, so the camera setup matters as much as this code.

Requires:  pip install opencv-python numpy requests
Run with:  python nikon_lightning.py
Stop with: Ctrl+C
"""

import os
import sys
import time
import glob

import cv2
import numpy as np
import requests

# ----------------------------- CONFIG ---------------------------------------

# digiCamControl web server. Enable it in:
#   File -> Settings -> Webserver  (tick "Enable"), then restart digiCamControl.
DCC = "http://localhost:5513"

# Where photos are saved. The script also tells digiCamControl to use this.
SAVE_FOLDER = r"C:\StormWatcher"

# "trigger" -> watch live view, shoot the instant a flash is seen.
#              Best for daytime / very active storms. NOTE: it will usually
#              MISS the flash that triggered it and instead catch what comes
#              right after (later return strokes, the afterglow, lit-up sky).
# "cull"     -> take back-to-back exposures and keep only the ones that
#              actually contain lightning. Best for NIGHT storms - this is the
#              mode that reliably captures strikes (the shutter is open when
#              they happen). Shoot JPEG so the script can read the files.
MODE = "trigger"

# --- detection tuning (same idea as the original webcam client) -------------
DIFF_PIXEL_THRESHOLD = 60    # per-pixel brightness jump that counts as "changed"
SCORE_TRIGGER        = 12.0  # fire when a frame's score crosses this
WARMUP_FRAMES        = 10    # ignore the first N frames while exposure settles

# --- shooting ---------------------------------------------------------------
AUTOFOCUS  = False  # False = CaptureNoAf (fast!). Pre-focus to infinity first.
BURST      = 1      # shots per detection. Try 2-3 to catch later strokes.
COOLDOWN_S = 1.0    # minimum seconds between detections

# --- cull mode only ---------------------------------------------------------
CULL_KEEP_SCORE = 8.0    # keep an exposure if it differs this much from the last
CULL_DELETE     = False  # False = move rejects to a "discard" folder (safe).
                         # True  = delete rejects outright.

# ----------------------------------------------------------------------------

HTTP = requests.Session()  # keep-alive connection -> lower trigger latency


def log(msg):
    print(f"[STORM] {msg}", flush=True)


def slc(cmd, param1="", param2="", timeout=10):
    """Send a digiCamControl 'single line command' and return its text reply."""
    r = HTTP.get(
        f"{DCC}/",
        params={"slc": cmd, "param1": param1, "param2": param2},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.text.strip()


def capture():
    """Fire the shutter as fast as possible."""
    cmd = "Capture" if AUTOFOCUS else "CaptureNoAf"
    for _ in range(max(1, BURST)):
        try:
            slc(cmd, timeout=30)
        except Exception as e:
            log(f"capture failed: {e}")


def start_liveview():
    try:
        slc("Do", "LiveViewWnd_Show")
        time.sleep(1.5)  # let the live-view window start streaming
    except Exception as e:
        log(f"could not start live view: {e}")


def stop_liveview():
    try:
        slc("Do", "LiveViewWnd_Hide")
    except Exception:
        pass


def liveview_frame():
    """Grab the current live-view JPEG and decode it to a BGR image (or None)."""
    for path in ("/liveview.jpg", "/liveviewwebcam.jpg"):
        try:
            r = HTTP.get(DCC + path, timeout=2)
            if r.status_code != 200 or not r.content:
                continue
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                return img
        except Exception:
            continue
    return None


def score(frame, prev):
    """How 'lightning-like' a frame is vs the previous one: a sudden, bright,
    wide change. Same heuristic as the original webcam client."""
    if frame is None or prev is None:
        return 0.0
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    if gray.shape != prev_gray.shape:
        return 0.0
    diff = cv2.absdiff(gray, prev_gray)
    hot_ratio = np.sum(diff > DIFF_PIXEL_THRESHOLD) / diff.size
    peak = float(np.percentile(diff, 99))
    return hot_ratio * 100 + peak * 0.5


def startup_checks():
    log("Booting lightning trigger...")

    # web server reachable?
    try:
        HTTP.get(DCC + "/", timeout=3)
        log("OK   digiCamControl web server reachable")
    except Exception as e:
        log(f"FAIL Can't reach digiCamControl at {DCC}.")
        log("     Is digiCamControl running with the web server enabled?")
        log(f"     ({e})")
        sys.exit(1)

    # camera responding? (soft check - param names vary between versions)
    try:
        name = slc("Get", "property.devicename")
        log(f"OK   Camera: {name or 'connected'}")
    except Exception as e:
        log(f"WARN No camera response yet - check USB cable / power. ({e})")

    # save folder
    os.makedirs(SAVE_FOLDER, exist_ok=True)
    try:
        slc("Set", "session.folder", SAVE_FOLDER)
        log(f"OK   Saving photos to {SAVE_FOLDER}")
    except Exception as e:
        log(f"WARN Could not set save folder: {e}")

    log(f"Mode: {MODE} | autofocus: {AUTOFOCUS} | burst: {BURST}")
    log("Ready. Watching for lightning... (Ctrl+C to stop)")


def run_trigger():
    """Watch live view; fire the shutter the instant a flash appears."""
    start_liveview()
    prev = None
    seen = 0
    last_fire = 0.0

    while True:
        frame = liveview_frame()
        if frame is None:
            time.sleep(0.1)
            continue

        seen += 1
        s = score(frame, prev)
        prev = frame

        if seen <= WARMUP_FRAMES:
            continue
        if s > 5:
            log(f"score={s:.1f}")

        if s > SCORE_TRIGGER and time.time() - last_fire > COOLDOWN_S:
            log(f"** LIGHTNING  score={s:.1f}  -> shooting")
            capture()
            last_fire = time.time()
            prev = None          # don't let the shot's own disturbance re-trigger
            start_liveview()     # resume live view after the still


def _newest_jpg(since):
    files = []
    for ext in ("*.jpg", "*.JPG", "*.jpeg", "*.JPEG"):
        files += glob.glob(os.path.join(SAVE_FOLDER, ext))
    files = [f for f in files if os.path.getmtime(f) > since]
    return max(files, key=os.path.getmtime) if files else None


def _wait_for_file(since, timeout=20):
    """Wait for a new jpg to appear and finish being written."""
    start = time.time()
    while time.time() - start < timeout:
        f = _newest_jpg(since)
        if f:
            size = -1
            while True:                      # wait until the size stops growing
                try:
                    s = os.path.getsize(f)
                except OSError:
                    s = -1
                if s == size and s > 0:
                    return f
                size = s
                time.sleep(0.15)
        time.sleep(0.1)
    return None


def run_cull():
    """Take exposures back to back; keep only the ones with lightning in them."""
    keep_dir = os.path.join(SAVE_FOLDER, "lightning")
    discard_dir = os.path.join(SAVE_FOLDER, "discard")
    os.makedirs(keep_dir, exist_ok=True)
    if not CULL_DELETE:
        os.makedirs(discard_dir, exist_ok=True)

    log("Continuous capture - keeping only frames with lightning.")
    log("Make sure the camera is shooting JPEG (the script must read each frame).")
    prev_small = None

    while True:
        t0 = time.time()
        capture()                       # one exposure
        f = _wait_for_file(t0)
        if not f:
            log("WARN No new file appeared - is the camera shooting JPEG?")
            continue

        img = cv2.imread(f)
        if img is None:                 # e.g. a RAW/NEF we can't read - leave it
            log(f"(can't read {os.path.basename(f)} - shoot JPEG for cull mode)")
            continue

        small = cv2.resize(img, (640, 426))
        s = score(small, prev_small)
        prev_small = small

        name = os.path.basename(f)
        if s > CULL_KEEP_SCORE:
            try:
                os.replace(f, os.path.join(keep_dir, name))
                log(f"** KEEP {name}  score={s:.1f}")
            except OSError as e:
                log(f"keep failed: {e}")
        elif CULL_DELETE:
            try:
                os.remove(f)
            except OSError:
                pass
        else:
            try:
                os.replace(f, os.path.join(discard_dir, name))
            except OSError:
                pass


def main():
    startup_checks()
    try:
        if MODE == "cull":
            run_cull()
        else:
            run_trigger()
    except KeyboardInterrupt:
        log("Stopping.")
    finally:
        stop_liveview()


if __name__ == "__main__":
    main()
