"""Night mode, driven by a stand-in tethered D3300 with real timing."""
import os
import threading
import time

import pytest

from stormwatch.night import NightController

EXP = 0.3


class Camera:
    """Confirms each USB release after `lag`, exposes, writes the picture to
    the card `write` s later, then 'downloads' it. Releases listed in
    `refuse` are ignored (busy camera); `lose` releases never produce a file."""

    def __init__(self, tmp, lag=0.05, write=0.05, raw=False, refuse=(), lose=()):
        self.tmp, self.lag, self.write, self.raw = tmp, lag, write, raw
        self.refuse, self.lose = set(refuse), set(lose)
        self.nc = None
        self.releases = 0
        self.counter = 0
        self.windows = {}      # stem -> (open, close)
        self.threads = []

    def fire(self):
        t = time.monotonic()
        self.releases += 1
        if self.releases not in self.refuse:
            th = threading.Thread(target=self._shoot, args=(self.releases,), daemon=True)
            th.start()
            self.threads.append(th)
        return t

    def _shoot(self, release):
        time.sleep(self.lag)
        self.counter += 1
        stem = f"DSC_{self.counter:04d}"
        t_open = time.monotonic()
        self.nc.on_release(t_open)
        time.sleep(EXP)
        self.windows[stem] = (t_open, time.monotonic())
        if release in self.lose:
            return
        time.sleep(self.write)
        exts = [".NEF", ".JPG"] if self.raw else [".JPG"]
        for ext in exts:
            self.nc.on_file_added(stem, time.monotonic())
        for ext in exts:
            p = os.path.join(self.tmp, stem + ext)
            with open(p, "w") as f:
                f.write("x")
            time.sleep(0.02)
            self.nc.on_file(p, stem)


def run(tmp_path, cam, n, flash_in=(), flash_at_gap=(), **kw):
    """Run n exposures; flash in the middle of the exposures listed (1-based
    controller exposure ids)."""
    events = []
    nc = NightController(cam.fire, EXP, 0.15, str(tmp_path), events.append,
                         confirm_release=True, file_events=True, **kw)
    cam.nc = nc
    nc.start()
    flashed = set()
    deadline = time.monotonic() + n * 2 + 5
    while time.monotonic() < deadline:
        e = nc.current
        if e and e["state"] == "open" and e["id"] in flash_in and e["id"] not in flashed:
            time.sleep(EXP / 2)
            nc.record_flash(time.monotonic())
            flashed.add(e["id"])
        done = [x for x in events if x["type"] == "night_exposure" and x["state"] != "open"]
        if len(done) >= n:
            break
        time.sleep(0.005)
    settle(nc, cam)
    return nc, events


def settle(nc, cam):
    """Disarm and wait for the open exposure to be judged and its files sorted."""
    nc.finish()
    nc._thread.join(5)
    for th in cam.threads:
        th.join(5)


def listing(tmp_path, sub):
    d = tmp_path / sub
    return sorted(os.listdir(d)) if d.exists() else []


def test_each_photo_is_judged_by_its_own_exposure(tmp_path):
    cam = Camera(str(tmp_path))
    run(tmp_path, cam, 4, flash_in={2})
    assert listing(tmp_path, "lightning") == ["DSC_0002.JPG"]
    # The dark frame right after the strike is NOT kept (the old cull bug).
    assert "DSC_0003.JPG" in listing(tmp_path, "no-lightning")


def test_back_to_back_strikes_are_both_kept(tmp_path):
    cam = Camera(str(tmp_path))
    run(tmp_path, cam, 4, flash_in={2, 3})
    assert listing(tmp_path, "lightning") == ["DSC_0002.JPG", "DSC_0003.JPG"]


def test_slow_card_and_raw_plus_jpeg_stay_together(tmp_path):
    cam = Camera(str(tmp_path), write=0.6, raw=True)
    run(tmp_path, cam, 3, flash_in={1})
    assert listing(tmp_path, "lightning") == ["DSC_0001.JPG", "DSC_0001.NEF"]
    assert listing(tmp_path, "no-lightning") == ["DSC_0002.JPG", "DSC_0002.NEF",
                                                 "DSC_0003.JPG", "DSC_0003.NEF"]


def test_ignored_release_does_not_shift_later_photos(tmp_path):
    cam = Camera(str(tmp_path), refuse={2})
    nc, events = run(tmp_path, cam, 4, flash_in={3})
    # Release 2 never happened, so exposure 3 produced the camera's 2nd file.
    assert listing(tmp_path, "lightning") == ["DSC_0002.JPG"]
    states = [e["state"] for e in events if e["type"] == "night_exposure" and e["state"] != "open"]
    assert "not released" in states


def test_lost_photo_does_not_shift_later_photos(tmp_path):
    cam = Camera(str(tmp_path), lose={2})
    nc, events = run(tmp_path, cam, 4, flash_in={3}, photo_timeout=1.0)
    assert listing(tmp_path, "lightning") == ["DSC_0003.JPG"]
    assert any(e.get("state") == "no photo" for e in events)


def test_disarm_still_sorts_the_open_exposure(tmp_path):
    cam = Camera(str(tmp_path))
    events = []
    nc = NightController(cam.fire, EXP, 0.15, str(tmp_path), events.append,
                         confirm_release=True, file_events=True)
    cam.nc = nc
    nc.start()
    while not (nc.current and nc.current["state"] == "open"):
        time.sleep(0.005)
    nc.record_flash(time.monotonic() + 0.01)
    settle(nc, cam)                   # disarmed mid-exposure
    assert listing(tmp_path, "lightning") == ["DSC_0001.JPG"]
    assert cam.releases == 1          # and no new exposure was started


def test_a_picture_taken_on_the_body_is_left_alone(tmp_path):
    cam = Camera(str(tmp_path))
    nc = NightController(cam.fire, EXP, 0.15, str(tmp_path), lambda e: None,
                         confirm_release=True, file_events=True)
    nc.on_file_added("DSC_9999", time.monotonic())   # nothing in flight
    p = tmp_path / "DSC_9999.JPG"
    p.write_text("x")
    nc.on_file(str(p), "DSC_9999")
    assert p.exists() and not (tmp_path / "lightning").exists()


def test_untethered_cable_mode_times_exposures_and_logs(tmp_path):
    fired = []
    events = []
    nc = NightController(lambda: fired.append(time.monotonic()) or fired[-1], EXP, 0.1,
                         str(tmp_path), events.append)
    nc.start()
    while len([e for e in events if e.get("state") == "open"]) < 2:
        time.sleep(0.005)
    nc.record_flash(time.monotonic() + 0.05)       # during exposure 2
    while len([e for e in events if e.get("state") == "done"]) < 3:
        time.sleep(0.005)
    nc.stop()
    done = [e for e in events if e.get("state") == "done"]
    assert [e["lightning"] for e in done[:3]] == [False, True, False]
    log = (tmp_path / "night-log.txt").read_text().splitlines()
    assert len(log) >= 3 and "LIGHTNING" in log[1]


def test_needs_a_timed_exposure(tmp_path):
    with pytest.raises(ValueError):
        NightController(lambda: 0.0, 0, 0.5, str(tmp_path), print)
