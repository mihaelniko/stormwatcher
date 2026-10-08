// Drives stormtrigger.ino through scenarios; prints one line per check.
#include "Arduino.h"
#include <cstdio>
#include <cstdlib>
uint32_t sim_us = 0;
int sim_pins[20];
std::deque<uint8_t> sim_samples;
SimSerial Serial;
#include "../../firmware/stormtrigger/stormtrigger.ino"

static const uint32_t PERIOD_US = 52;  // ~19.2 kHz
static int fails = 0;
#define CHECK(cond, name) do { bool ok_ = (cond); printf("%s %s\n", ok_ ? "PASS" : "FAIL", name); if (!ok_) fails++; } while (0)

static uint32_t first_fire_us;
// Run n samples at `level` (+-noise); returns true if the shutter closed.
static bool feed(int n, int level, int noise = 2) {
  bool fired = false;
  for (int i = 0; i < n; i++) {
    int s = level + (noise ? (rand() % (2 * noise + 1)) - noise : 0);
    sim_samples.push_back((uint8_t)std::max(0, std::min(255, s)));
    loop();
    if (sim_pins[PIN_SHUTTER] && !fired) { fired = true; first_fire_us = sim_us; }
    sim_us += PERIOD_US;
  }
  return fired;
}
static std::string take() { std::string s = Serial.out; Serial.out.clear(); return s; }

int main() {
  srand(1);
  setup();
  CHECK(take() == "HELLO STORMTRIGGER 1 GENERIC\n", "hello line");
  CHECK(!feed(19200, 60), "no fire at boot (auto off, warming up)");
  Serial.in = "A";
  CHECK(!feed(19200, 60), "armed: 1 s of noisy ambient light does not fire");

  uint32_t t_flash = sim_us;
  bool fired = feed(10, 140, 0);
  CHECK(fired && first_fire_us == t_flash, "flash closes the shutter on the first bright sample");
  CHECK(sim_pins[PIN_FOCUS] == HIGH, "focus closed together with shutter");
  std::string out = take();
  CHECK(out.rfind("F ", 0) == 0 && out.find(" P ") != std::string::npos, "F line reports photodiode source");

  feed(2000, 140, 0);  // flash lasts 100 ms
  CHECK(sim_pins[PIN_SHUTTER] == HIGH, "still pressed inside the 150 ms hold");
  feed(1000, 60);
  CHECK(sim_pins[PIN_SHUTTER] == LOW && sim_pins[PIN_FOCUS] == LOW, "released after hold");
  CHECK(!feed(500, 140, 0), "second flash inside the 600 ms re-arm window is ignored");
  feed(12000, 60);
  CHECK(feed(10, 140, 0), "fires again after re-arm");
  feed(20000, 60);
  take();

  Serial.in = "a";
  feed(10, 60);
  CHECK(!feed(10, 160, 0), "disarmed: photodiode cannot fire");
  feed(20000, 60);
  Serial.in = "t";
  CHECK(feed(1, 60), "host 't' fires within the same loop pass");
  out = take();
  CHECK(out.find(" H ") != std::string::npos, "F line reports host source");
  feed(20000, 60);

  Serial.in = "W";
  feed(5, 60);
  CHECK(sim_pins[PIN_FOCUS] == HIGH && sim_pins[PIN_SHUTTER] == LOW, "W holds focus only");
  Serial.in = "t";
  feed(5000, 60);
  CHECK(sim_pins[PIN_FOCUS] == HIGH, "focus stays held after a release while awake");
  Serial.in = "w";
  feed(5, 60);
  CHECK(sim_pins[PIN_FOCUS] == LOW, "w releases focus");
  take();

  Serial.in = "S20,8,200,700\n";
  feed(5, 60);
  CHECK(take() == "OK\n", "S command acknowledged");
  Serial.in = "?";
  feed(1, 60);
  out = take();
  int a, w, tl, thr, k, hold, rearm, base, dev;
  int n = sscanf(out.c_str(), "S %d %d %d %d %d %d %d %d %d", &a, &w, &tl, &thr, &k, &hold, &rearm, &base, &dev);
  CHECK(n == 9 && thr == 20 && k == 8 && hold == 200 && rearm == 700, "status reflects new parameters");
  CHECK(base >= 57 && base <= 63, "baseline tracks ambient level");

  Serial.in = "M";
  feed(19, 60);  // ~1 ms
  take();
  feed(1923, 60);  // 100 ms
  out = take();
  int lines = 0;
  for (char c : out) lines += c == '\n';
  CHECK(lines >= 9 && lines <= 11 && out.rfind("T ", 0) == 0, "telemetry every 10 ms");
  Serial.in = "mA";  // armed: light comes up and stays (a lamp, the sun)
  feed(20000, 60);   // let the re-arm window from the last release pass
  take();
  int releases = 0;
  bool was = false;
  for (int i = 0; i < 19200 * 8; i++) {
    feed(1, 150, 1);
    if (sim_pins[PIN_SHUTTER] && !was) releases++;
    was = sim_pins[PIN_SHUTTER];
  }
  CHECK(releases == 1, "a lasting brightness step releases once, not every re-arm");
  Serial.in = "a";
  feed(19200 * 60, 252, 1);  // sensor pegged near full scale for a minute
  take();
  Serial.in = "M";
  feed(400, 252, 1);
  out = take();
  CHECK(out.find(" 256\n") != std::string::npos, "saturated sensor reported as trip 256");
  printf("%s\n", fails ? "FIRMWARE FAIL" : "FIRMWARE OK");
  return fails ? 1 : 0;
}
