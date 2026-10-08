import os
import time

import pytest

import fake_gphoto2 as fgp
from stormwatch.camera import CameraWorker


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def cam(tmp_path):
    fgp.DEVICE = fgp.Device()
    events = []
    w = CameraWorker(events.append, gp=fgp, dest_dir=str(tmp_path), download="jpeg")
    w.events = events
    w.start()
    assert wait_for(lambda: w.state == "ready")
    yield w
    w.stop()


def of(events, kind):
    return [e for e in events if e.get("type") == kind]


def test_connect_sets_card_target_and_no_autofocus(cam):
    d = fgp.DEVICE
    assert d.settings["capturetarget"][0] == "Memory card"
    assert d.settings["autofocus"][0] == "Off"
    snap = [e for e in cam.events if e.get("type") == "camera" and "settings" in e][-1]
    assert snap["settings"]["iso"]["choices"][0] == "100"
    assert snap["status"]["batterylevel"] == "80%"
    assert snap["warnings"] == []


def test_fast_release_is_one_ptp_transaction(cam):
    cam.prepare_trigger()
    t = cam.request_trigger()
    assert wait_for(lambda: of(cam.events, "camera_fired"))
    fired = of(cam.events, "camera_fired")[0]
    assert fired["method"] == "direct"
    d = fgp.DEVICE
    assert (0x90C2, 1) in d.opcodes                      # PC control mode, once
    assert (0x9207, 0xFFFFFFFF, 0) in d.opcodes          # capture, no AF, to card
    assert d.std_triggers == 0
    assert fired["t_accepted"] - t < 0.05                # no 100 ms busy polling


def test_busy_camera_is_retried_quickly(cam):
    fgp.DEVICE.busy_responses = 5
    cam.request_trigger()
    assert wait_for(lambda: of(cam.events, "camera_fired"))
    assert of(cam.events, "camera_fired")[0]["method"] == "direct"
    assert fgp.DEVICE.std_triggers == 0


def test_one_parameter_bodies(cam):
    fgp.DEVICE.one_param_only = True
    cam.request_trigger()
    assert wait_for(lambda: of(cam.events, "camera_fired"))
    assert (0x9207, 0xFFFFFFFF) in fgp.DEVICE.opcodes


def test_falls_back_to_libgphoto2_when_unsupported(cam):
    fgp.DEVICE.direct_supported = False
    cam.request_trigger()
    assert wait_for(lambda: of(cam.events, "camera_fired"))
    assert of(cam.events, "camera_fired")[0]["method"] == "libgphoto2"
    n_opcodes = len([x for x in fgp.DEVICE.log if x[0] == "opcode"])
    cam.request_trigger()
    assert wait_for(lambda: len(of(cam.events, "camera_fired")) == 2)
    assert len([x for x in fgp.DEVICE.log if x[0] == "opcode"]) == n_opcodes  # not retried


def test_downloads_jpeg_only(cam, tmp_path):
    fgp.DEVICE.raw_plus_jpeg = True
    cam.request_trigger()
    assert wait_for(lambda: of(cam.events, "file"), timeout=5)
    files = of(cam.events, "file")
    assert len(files) == 1 and files[0]["name"].endswith(".JPG")
    path = files[0]["path"]
    assert os.path.getsize(path) == len(fgp.DEVICE.files[("/store_00010001/DCIM/100D3300", files[0]["name"])])
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".part")]


def test_release_preempts_a_running_download(cam):
    d = fgp.DEVICE
    d.read_delay = 0.15
    cam.request_trigger()
    assert wait_for(lambda: any(x[0] == "read" for x in d.log), timeout=5)
    cam.request_trigger()  # arrives mid-download
    assert wait_for(lambda: len(of(cam.events, "camera_fired")) == 2, timeout=5)
    log = d.log
    first_read = next(i for i, x in enumerate(log) if x[0] == "read")
    second_release = [i for i, x in enumerate(log) if x[0] == "opcode" and x[1][0] == 0x9207][-1]
    last_read_first_file = max(i for i, x in enumerate(log) if x[0] == "read" and x[1] == "DSC_0001.JPG")
    assert first_read < second_release < last_read_first_file


def test_unplug_and_replug(cam):
    fgp.DEVICE.io_error_next = True
    assert wait_for(lambda: cam.state == "searching", timeout=3)
    fgp.DEVICE.present = True
    assert wait_for(lambda: cam.state == "ready", timeout=5)


def test_claimed_by_another_program(tmp_path):
    fgp.DEVICE = fgp.Device()
    fgp.DEVICE.claim_error = True
    events = []
    w = CameraWorker(events.append, gp=fgp, dest_dir=str(tmp_path))
    w.start()
    try:
        assert wait_for(lambda: w.state == "blocked")
        assert "holders" in [e for e in events if e.get("state") == "blocked"][0]
    finally:
        w.stop()


def test_liveview_frames_and_inline_release(cam):
    frames = []

    def on_frame(f):
        frames.append(f)
        if len(frames) == 3:
            cam.request_trigger()

    cam.set_liveview(on_frame)
    assert wait_for(lambda: len(frames) >= 5)
    assert frames[0].luma.ndim == 2 and 160 <= frames[0].luma.shape[1] <= 320
    assert of(cam.events, "camera_fired")
    log = fgp.DEVICE.log
    third_preview = [i for i, x in enumerate(log) if x[0] == "preview"][2]
    release = next(i for i, x in enumerate(log) if x[0] == "opcode" and x[1][0] == 0x9207)
    assert release == third_preview + 1 or log[third_preview + 1][0] == "opcode"
    cam.set_liveview(None)
    assert wait_for(lambda: ("viewfinder", 0) in fgp.DEVICE.log)


def test_warnings_for_af_lens_and_auto_mode(cam):
    fgp.DEVICE.settings["focusmode"][0] = "AF-S"
    fgp.DEVICE.settings["expprogram"][0] = "A"
    cam.refresh()
    assert wait_for(lambda: [e for e in cam.events if e.get("warnings")])
    w = [e for e in cam.events if e.get("warnings")][-1]["warnings"]
    assert len(w) == 2


def test_set_config(cam):
    cam.set_config("iso", "1600")
    assert wait_for(lambda: fgp.DEVICE.settings["iso"][0] == "1600")
    assert cam.call(lambda: cam.info["settings"]["iso"]["value"]) == "1600"
