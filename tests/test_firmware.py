"""Firmware checks.

* Host simulation: the sketch compiled for x86 against tests/firmware/Arduino.h
  and driven sample-by-sample through flash / noise / protocol scenarios.
* Real AVR build (optional): set STORMWATCH_ARDUINO_CORE to a checkout of
  https://github.com/arduino/ArduinoCore-avr and have avr-gcc installed.
"""
import glob
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKETCH = os.path.join(ROOT, "firmware", "stormtrigger", "stormtrigger.ino")
SIM = os.path.join(ROOT, "tests", "firmware")


def test_firmware_logic_in_host_simulation(tmp_path):
    cxx = shutil.which("g++") or shutil.which("c++")
    if not cxx:
        pytest.skip("needs a C++ compiler")
    exe = tmp_path / "fwsim"
    subprocess.run([cxx, "-std=gnu++17", "-O1", "-Wall", "-Wno-unused-function", f"-I{SIM}",
                    "-o", str(exe), os.path.join(SIM, "sim_main.cpp")], check=True)
    res = subprocess.run([str(exe)], capture_output=True, text=True)
    print(res.stdout)
    assert res.returncode == 0 and "FIRMWARE OK" in res.stdout


def test_firmware_builds_for_atmega328p(tmp_path):
    core = os.environ.get("STORMWATCH_ARDUINO_CORE")
    if not core or not shutil.which("avr-gcc"):
        pytest.skip("set STORMWATCH_ARDUINO_CORE and install avr-gcc")
    flags = ["-mmcu=atmega328p", "-DF_CPU=16000000L", "-DARDUINO=10819", "-DARDUINO_AVR_UNO",
             "-DARDUINO_ARCH_AVR", "-Os", "-ffunction-sections", "-fdata-sections", "-flto",
             f"-I{core}/cores/arduino", f"-I{core}/variants/standard"]
    cxxflags = ["-std=gnu++11", "-fno-exceptions", "-fno-threadsafe-statics"]
    objs = []
    for src in glob.glob(f"{core}/cores/arduino/*.c"):
        o = tmp_path / (os.path.basename(src) + ".o")
        subprocess.run(["avr-gcc", *flags, "-std=gnu11", "-c", src, "-o", str(o)], check=True)
        objs.append(str(o))
    for src in glob.glob(f"{core}/cores/arduino/*.cpp"):
        o = tmp_path / (os.path.basename(src) + ".o")
        subprocess.run(["avr-g++", *flags, *cxxflags, "-c", src, "-o", str(o)], check=True)
        objs.append(str(o))
    for src in glob.glob(f"{core}/cores/arduino/*.S"):
        o = tmp_path / (os.path.basename(src) + ".o")
        subprocess.run(["avr-gcc", *flags, "-x", "assembler-with-cpp", "-c", src, "-o", str(o)], check=True)
        objs.append(str(o))
    cpp = tmp_path / "sketch.cpp"
    shutil.copy(SKETCH, cpp)
    res = subprocess.run(["avr-g++", *flags, *cxxflags, "-Wall", "-Wextra", "-c", str(cpp),
                          "-o", str(tmp_path / "sketch.o")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "warning" not in res.stderr, res.stderr
    elf = tmp_path / "stormtrigger.elf"
    subprocess.run(["avr-gcc", *flags, "-Wl,--gc-sections", "-o", str(elf), *objs,
                    str(tmp_path / "sketch.o"), "-lm"], check=True)
    size = subprocess.run(["avr-size", "-C", "--mcu=atmega328p", str(elf)],
                          capture_output=True, text=True).stdout
    print(size)
    assert "Program" in size
