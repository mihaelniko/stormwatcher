import os

import pytest

from stormwatch.night import LAG_MAX, NightController


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(tmp_path, exposure=1.0):
    events = []
    nc = NightController(shutter=None, exposure_s=exposure, gap_s=0.5, out_dir=str(tmp_path),
                         emit=events.append, clock=Clock())
    return nc, events


def touch(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def interval(nc):
    return nc.exposure + LAG_MAX + 0.5


def test_dark_frame_after_a_strike_is_not_kept(tmp_path):
    """The old cull mode compared each frame with the previous one and kept
    the dark frame that follows a strike. Here each exposure stands alone."""
    nc, _ = make(tmp_path)
    t0 = 100.0
    e1 = nc.record_exposure(t0)
    nc.record_flash(t0 + 0.6)                       # strike inside exposure 1
    e2 = nc.record_exposure(t0 + interval(nc))      # dark
    nc.on_file(touch(tmp_path, "DSC_0001.JPG"), "DSC_0001", e1["close"] + 0.4)
    nc.on_file(touch(tmp_path, "DSC_0002.JPG"), "DSC_0002", e2["close"] + 0.4)
    assert os.path.exists(tmp_path / "lightning" / "DSC_0001.JPG")
    assert os.path.exists(tmp_path / "no-lightning" / "DSC_0002.JPG")


def test_back_to_back_strikes_are_both_kept(tmp_path):
    nc, _ = make(tmp_path)
    t = 100.0
    for i in range(3):
        nc.record_exposure(t + i * interval(nc))
    nc.record_flash(100.5)
    nc.record_flash(100.0 + interval(nc) + 0.5)
    for i in range(3):
        name = f"DSC_000{i + 1}.JPG"
        nc.on_file(touch(tmp_path, name), name[:-4], 100.0 + i * interval(nc) + 2.0)
    assert sorted(os.listdir(tmp_path / "lightning")) == ["DSC_0001.JPG", "DSC_0002.JPG"]
    assert os.listdir(tmp_path / "no-lightning") == ["DSC_0003.JPG"]


def test_flash_reported_before_its_exposure_is_recorded_still_counts(tmp_path):
    nc, _ = make(tmp_path)
    nc.record_flash(100.2)            # detector thread got there first
    e = nc.record_exposure(100.0)
    assert e["flash"]


def test_raw_and_jpeg_of_one_exposure_share_the_verdict(tmp_path):
    nc, _ = make(tmp_path)
    nc.record_exposure(100.0)
    nc.record_flash(100.3)
    nc.on_file(touch(tmp_path, "DSC_0001.NEF"), "DSC_0001", 102.0)
    nc.on_file(touch(tmp_path, "DSC_0001.JPG"), "DSC_0001", 102.2)
    assert sorted(os.listdir(tmp_path / "lightning")) == ["DSC_0001.JPG", "DSC_0001.NEF"]


def test_skipped_release_does_not_shift_later_frames(tmp_path):
    nc, _ = make(tmp_path)
    iv = interval(nc)
    nc.record_exposure(100.0)               # camera ignored this one (no file ever)
    for i in range(1, 30):
        nc.record_exposure(100.0 + i * iv)
    nc.record_flash(100.0 + 25 * iv + 0.5)
    # Files for exposures 2..26 arrive; exposure 1 never produces one.
    for i in range(1, 26):
        name = f"DSC_{i:04d}"
        nc.on_file(touch(tmp_path, name + ".JPG"), name, 100.0 + i * iv + nc.exposure + LAG_MAX + 0.5)
    # Exposure 26 held the flash; its file is DSC_0025 because exposure 1
    # produced nothing. Exactly that frame is kept.
    assert os.listdir(tmp_path / "lightning") == ["DSC_0025.JPG"]


def test_needs_a_timed_exposure(tmp_path):
    with pytest.raises(ValueError):
        NightController(None, 0, 0.5, str(tmp_path), print)
