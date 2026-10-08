"""End-to-end engine runs with a simulated storm."""
import os
import time

import pytest

import fake_gphoto2 as fgp
from stormwatch.camera import CameraWorker
from stormwatch.engine import Engine
from stormwatch.settings import Settings
from stormwatch.simulate import SimulatedSource
from stormwatch.tty import SerialPort


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Events(list):
    def of(self, kind):
        return [e for e in list(self) if e.get("type") == kind]


def engine(tmp_path, every=(0.4, 0.7), **overrides):
    s = Settings(detector="simulated", shutter="none", output_dir=str(tmp_path),
                 realtime=False, cooldown_ms=200)
    for k, v in overrides.items():
        setattr(s, k, v)
    ev = Events()
    kw = dict(enable_camera=False, list_serial=lambda: [], list_video=lambda: [],
              sim_factory=lambda: SimulatedSource(every=every, seed=3))
    return Engine(s, ev.append, **kw), ev


def test_triggers_only_while_armed_and_fast(tmp_path):
    eng, ev = engine(tmp_path)
    eng.start()
    try:
        time.sleep(1.5)
        assert not ev.of("trigger")
        eng.arm()
        assert wait_for(lambda: len(ev.of("trigger")) >= 2, timeout=6)
        trig = ev.of("trigger")
        assert all(t["source"] == "simulated" for t in trig)
        assert all(t["reaction_ms"] is not None and t["reaction_ms"] < 50 for t in trig)
        eng.disarm()
        n = len(ev.of("trigger"))
        time.sleep(1.5)
        assert len(ev.of("trigger")) == n
        tele = ev.of("telemetry")
        assert tele and 30 < tele[-1]["fps"] < 90
        assert tele[-1]["proc_ms"] < 10
    finally:
        eng.shutdown()


def test_cooldown_limits_release_rate(tmp_path):
    eng, ev = engine(tmp_path, every=(0.15, 0.25), cooldown_ms=1500)
    eng.start()
    try:
        time.sleep(1.0)
        eng.arm()
        time.sleep(3.2)
        times = [t["t_sent"] for t in ev.of("trigger")]
        assert 1 <= len(times) <= 3
        assert all(b - a >= 1.5 for a, b in zip(times, times[1:]))
    finally:
        eng.shutdown()


def test_detector_clip_saved_around_trigger(tmp_path):
    eng, ev = engine(tmp_path)
    eng.start()
    try:
        time.sleep(1.0)
        eng.arm()
        assert wait_for(lambda: ev.of("clip"), timeout=8)
        clip = ev.of("clip")[0]
        assert clip["frames"] >= 5 and os.path.exists(clip["trigger_frame"])
        assert os.path.basename(clip["trigger_frame"]).startswith("T")
    finally:
        eng.shutdown()


def test_test_fire_and_state(tmp_path):
    eng, ev = engine(tmp_path)
    eng.start()
    try:
        eng.handle({"cmd": "test_fire"})
        assert ev.of("trigger")[0]["source"] == "test"
        st = ev.of("state")[-1]
        assert st["detector"]["kind"] == "simulated" and st["shutter"]["kind"] == "none"
        eng.handle({"cmd": "nonsense"})  # ignored, no exception
        eng.handle({"cmd": "apply", "settings": {**eng.s.to_dict(), "sensitivity": 0.2}})
        assert eng.detector.sensitivity == pytest.approx(0.2)
    finally:
        eng.shutdown()


def test_night_mode_demo_runs_timed_exposures(tmp_path):
    eng, ev = engine(tmp_path, every=(0.3, 0.5), mode="night", night_gap_s=0.1)
    eng.start()
    try:
        time.sleep(1.0)
        assert eng.arm()                      # demo: 2 s exposures without a camera
        time.sleep(5.0)
        done = [e for e in ev.of("night_exposure") if e["state"] == "done"]
        assert 1 <= len(done) <= 3
        assert not ev.of("trigger")           # flashes don't release in night mode
        assert ev.of("night_flash")
    finally:
        eng.shutdown()


def test_arming_never_fails_silently(tmp_path):
    s = Settings(detector="simulated", shutter="usb", mode="night", output_dir=str(tmp_path),
                 realtime=False)
    ev = Events()
    fgp.DEVICE = fgp.Device()
    fgp.DEVICE.present = False                # camera unplugged
    eng = Engine(s, ev.append, list_video=lambda: [], list_serial=lambda: [],
                 sim_factory=lambda: SimulatedSource(seed=1),
                 camera_factory=lambda emit: CameraWorker(emit, gp=fgp, dest_dir=str(tmp_path)))
    eng.start()
    try:
        assert not eng.arm()
        reason = ev.of("arm_failed")[-1]["reason"]
        assert "exposure" in reason and "Night exposure" in reason
        eng.s.night_exposure_s = 4.0          # exposure known, but USB release needs the camera
        assert not eng.arm()
        assert "connected" in ev.of("arm_failed")[-1]["reason"]
        fgp.DEVICE.present = True
        fgp.DEVICE.settings["shutterspeed"][0] = "Bulb"
        eng.s.night_exposure_s = 0.0
        assert wait_for(lambda: eng.camera.state == "ready")
        assert wait_for(lambda: "settings" in eng.camera.info)
        assert not eng.arm()
        assert "Bulb" in ev.of("arm_failed")[-1]["reason"]
        assert not eng.armed
    finally:
        eng.shutdown()


@pytest.mark.parametrize("write,raw", [(0.02, False), (0.7, False), (0.3, True)])
def test_night_mode_sorts_every_photo_by_its_own_exposure(tmp_path, write, raw):
    """Ground truth: the simulated D3300 records when its shutter was really
    open; the simulated storm records when every stroke began."""
    fgp.DEVICE = fgp.Device()
    d = fgp.DEVICE
    d.settings["shutterspeed"][0] = "10/13"   # how libgphoto2 spells 0.77 s on Nikon
    d.exposure, d.lag, d.write_delay, d.raw_plus_jpeg = 10 / 13, 0.1, write, raw
    sim = SimulatedSource(every=(1.4, 3.2), seed=3)
    s = Settings(detector="simulated", shutter="usb", mode="night", output_dir=str(tmp_path),
                 realtime=False, night_gap_s=0.2, download="all" if raw else "jpeg")
    ev = Events()
    eng = Engine(s, ev.append, list_video=lambda: [], list_serial=lambda: [], sim_factory=lambda: sim,
                 camera_factory=lambda emit: CameraWorker(emit, gp=fgp, dest_dir=eng.session_dir,
                                                          download=s.download))
    eng.start()
    try:
        assert wait_for(lambda: eng.camera.state == "ready" and "settings" in eng.camera.info)
        time.sleep(0.8)
        assert eng.arm()
        time.sleep(12.0)
        eng.disarm()
        assert wait_for(lambda: len({os.path.basename(e["path"]) for e in ev.of("night_file")})
                        == len(d.windows) * (2 if raw else 1), timeout=8)
    finally:
        eng.shutdown()

    sorted_ = {}
    for e in ev.of("night_file"):
        stem = "DSC_" + os.path.basename(e["path"]).split("DSC_")[1].split(".")[0]
        sorted_.setdefault(stem, set()).add(e["lightning"])
    truth, ambiguous = {}, set()
    for i, (o, c) in enumerate(d.windows, 1):
        stem = f"DSC_{i:04d}"
        truth[stem] = any(o <= f <= c for f in sim.flash_times)
        if any(o - 0.3 <= f < o or c < f <= c + 0.25 for f in sim.flash_times):
            ambiguous.add(stem)  # a stroke within the timing slack: keeping it is fine
    assert sum(truth.values()) >= 1 and len(truth) - sum(truth.values()) >= 1
    for stem, verdicts in sorted_.items():
        assert len(verdicts) == 1, f"{stem}: RAW and JPEG sorted apart"
        got = next(iter(verdicts))
        if truth[stem]:
            assert got, f"{stem} held lightning but was not kept"
        elif stem not in ambiguous:
            assert not got, f"{stem} had no lightning but was kept"


class FakeLink:
    def __init__(self, path, on_fire, on_telemetry, on_log):
        self.path, self.on_fire, self.on_tele = path, on_fire, on_telemetry
        self.sent = []
        self.connected = False
        self.board = "328P"

    def open(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False

    def configure(self, *a):
        self.sent.append(("S",) + a)

    def set_auto(self, on):
        self.sent.append(("A", on))

    def set_awake(self, on):
        self.sent.append(("W", on))

    def set_telemetry(self, on):
        self.sent.append(("M", on))

    def fire(self):
        self.sent.append(("t",))
        return time.monotonic()


def test_photodiode_board_fires_in_hardware(tmp_path):
    links = []

    def factory(path, **kw):
        links.append(FakeLink(path, **kw))
        return links[-1]

    s = Settings(detector="photodiode", shutter="arduino", output_dir=str(tmp_path), realtime=False)
    ev = Events()
    eng = Engine(s, ev.append, enable_camera=False, link_factory=factory, list_video=lambda: [],
                 list_serial=lambda: [SerialPort("/dev/ttyACM0", vid="2341", product="Arduino Uno")])
    eng.start()
    try:
        link = links[0]
        assert ("M", True) in link.sent
        eng.arm()
        assert ("A", True) in link.sent and ("W", True) in link.sent
        link.on_tele(40, 20, 44, time.monotonic())
        link.on_fire({"src": "P", "t": time.monotonic(), "micros": 1, "level": 120, "trip": 44})
        trig = ev.of("trigger")[-1]
        assert trig["hardware"] and trig["source"] == "photodiode" and trig["reaction_ms"] is None
        assert ("t",) not in link.sent      # the board already released; the PC must not
        eng.disarm()
        assert link.sent[-2:] in ([("A", False), ("W", False)], [("W", False), ("A", False)]) or \
            ("A", False) in link.sent[-3:]
    finally:
        eng.shutdown()


def test_usb_release_end_to_end_with_downloads(tmp_path):
    fgp.DEVICE = fgp.Device()
    s = Settings(detector="simulated", shutter="usb", output_dir=str(tmp_path), realtime=False,
                 cooldown_ms=300)
    ev = Events()
    eng = Engine(s, ev.append, list_video=lambda: [], list_serial=lambda: [],
                 sim_factory=lambda: SimulatedSource(every=(0.5, 0.8), seed=5),
                 camera_factory=lambda emit: CameraWorker(emit, gp=fgp, dest_dir=eng.session_dir))
    eng.start()
    try:
        assert wait_for(lambda: eng.camera.state == "ready")
        time.sleep(1.0)
        eng.arm()
        assert wait_for(lambda: ev.of("file"), timeout=8)
        fired = ev.of("camera_fired")
        assert fired and fired[0]["method"] == "direct"
        assert any(op[0] == 0x9207 for op in fgp.DEVICE.opcodes)
        assert os.path.exists(ev.of("file")[0]["path"])
    finally:
        eng.shutdown()
