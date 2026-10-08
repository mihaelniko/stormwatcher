// Minimal host-side Arduino environment for simulating stormtrigger.ino.
// The sketch's generic (non-328P) path samples via analogRead() in loop(), so
// each loop() call is one ADC sample; the clock advances one sample period.
#pragma once
#include <stdint.h>
#include <string>
#include <deque>
#include <algorithm>
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define A0 14
#define F(x) (x)
#define constrain(x, lo, hi) ((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x)))
template <class A, class B> static inline A max(A a, B b) { return a > (A)b ? a : (A)b; }
extern uint32_t sim_us;
extern int sim_pins[20];
extern std::deque<uint8_t> sim_samples;
static inline uint32_t micros() { return sim_us; }
static inline uint32_t millis() { return sim_us / 1000; }
static inline void pinMode(int, int) {}
static inline void digitalWrite(int p, int v) { sim_pins[p] = v; }
static inline int analogRead(int) {
  uint8_t s = sim_samples.empty() ? 0 : sim_samples.front();
  if (!sim_samples.empty()) sim_samples.pop_front();
  return s << 2;
}
static inline void noInterrupts() {}
static inline void interrupts() {}
struct SimSerial {
  std::string in, out;
  void begin(long) {}
  int available() { return (int)in.size(); }
  int read() { if (in.empty()) return -1; int c = (uint8_t)in[0]; in.erase(0, 1); return c; }
  void print(const char *s) { out += s; }
  void print(char c) { out += c; }
  void print(long v) { out += std::to_string(v); }
  void print(unsigned long v) { out += std::to_string(v); }
  void print(int v) { out += std::to_string(v); }
  void print(unsigned v) { out += std::to_string(v); }
  void println(const char *s) { out += s; out += "\n"; }
  void println(long v) { print(v); out += "\n"; }
  void println(unsigned long v) { print(v); out += "\n"; }
  void println(int v) { print(v); out += "\n"; }
  void println(unsigned v) { print(v); out += "\n"; }
};
extern SimSerial Serial;
