/*
  StormTrigger - lightning trigger firmware for Nikon MC-DC2 cameras (D3300 etc.)
  ===============================================================================

  Two jobs, both on the camera's MC-DC2 remote port via optocouplers:

  1. Photodiode trigger (fastest possible). The ADC free-runs at ~19 kHz and
     every sample is compared against an adaptive trip level inside the ADC
     interrupt. When the sky flashes, the interrupt drives the SHUTTER and
     FOCUS optocouplers by direct port write: light-to-shutter-line < 60 us.
  2. Remote cable for the StormWatch app: the PC sends one byte 't' and the
     shutter line closes within microseconds of the byte arriving.

  While armed, FOCUS (half-press) can be held so the D3300 never drops into
  standby; a camera that is already awake releases noticeably faster.

  Wiring (Uno / Nano / Pro Mini, ATmega328P):
    A0  <- phototransistor emitter + load resistor to GND (collector to 5V)
    D2  -> 330R -> PC817 #1 LED; PC817 #1 C/E across MC-DC2 SHUTTER and GND
    D3  -> 330R -> PC817 #2 LED; PC817 #2 C/E across MC-DC2 FOCUS and GND
    D13    on-board LED mirrors the shutter line
  Other boards fall back to a polled analogRead loop (slower, same protocol).

  Serial 115200 8N1, protocol 1 (see stormwatch/arduino.py):
    host -> board  t  A/a  W/w  M/m  ?  S<thr>,<k>,<hold_ms>,<rearm_ms>\n
    board -> host  HELLO STORMTRIGGER 1 <board>
                   F <micros> <P|H> <level> <trip>
                   T <max> <base> <trip>          (every 10 ms when enabled)
                   S <auto> <awake> <tele> <thr> <k> <hold> <rearm> <base> <dev16>
                   OK | E <message>

  Standalone use (no PC, e.g. on a power bank): set AUTO_AT_BOOT to 1 and the
  photodiode trigger is armed from power-on.
*/

#include <Arduino.h>
#include <stdlib.h>

#define PROTOCOL 1
#define AUTO_AT_BOOT 0
#define AWAKE_AT_BOOT 0

static const uint8_t PIN_SHUTTER = 2;
static const uint8_t PIN_FOCUS = 3;
static const uint8_t PIN_LED = 13;

// ---- parameters (host-settable) ------------------------------------------
static uint8_t  p_thr_min = 12;   // minimum rise above baseline (8-bit ADC counts)
static uint8_t  p_k = 6;          // ...and at least k x mean deviation (noise)
static uint16_t p_hold_ms = 150;  // how long the shutter line stays closed
static uint16_t p_rearm_ms = 600; // photodiode dead time after a release

// ---- state shared with the ADC interrupt ---------------------------------
volatile uint8_t  s_auto = AUTO_AT_BOOT;  // photodiode may fire
volatile uint8_t  s_awake = AWAKE_AT_BOOT;
volatile uint8_t  g_trip = 255;     // fire when a sample exceeds this...
volatile uint8_t  g_trip_ok = 0;    // ...and the baseline is valid
volatile uint8_t  g_was_above = 1;  // previous sample above trip: fire only on a fresh crossing
volatile uint8_t  g_lockout = 0;    // pressing or re-arming: ISR must not fire
volatile uint8_t  g_pressing = 0;
volatile uint8_t  g_fired = 0;      // a release happened; loop() reports it
volatile uint8_t  g_fire_src = 'P';
volatile uint8_t  g_fire_level = 0;
volatile uint32_t g_fire_us = 0;
volatile uint32_t g_press_ms = 0;
volatile uint8_t  g_last = 0;       // latest sample
volatile uint8_t  g_max = 0;        // max sample since last telemetry line
volatile uint8_t  g_base8 = 0;      // baseline copy for the ISR's deviation sum
volatile uint16_t g_acc = 0, g_devacc = 0;
volatile uint8_t  g_n = 0;
volatile uint8_t  g_block_ready = 0, g_block_mean = 0, g_block_dev = 0;

// baseline / noise, owned by loop(), 16.16 fixed point
static uint32_t base32 = 0;   // baseline (EMA of 16-sample block means)
static uint32_t dev32 = 0;    // mean |sample - baseline|
static uint16_t blocks = 0;   // warm-up counter
static uint8_t  s_tele = 0;
static uint32_t lockout_until = 0;
static uint32_t last_tele = 0;

#if defined(__AVR_ATmega328P__) || defined(__AVR_ATmega168__)
#define FAST_PATH 1
static inline void lines_press() { PORTD |= _BV(PD2) | _BV(PD3); PORTB |= _BV(PB5); }
static inline void shutter_open() { PORTD &= ~_BV(PD2); PORTB &= ~_BV(PB5); }
static inline void focus_set(uint8_t on) { if (on) PORTD |= _BV(PD3); else PORTD &= ~_BV(PD3); }
#else
#define FAST_PATH 0
static inline void lines_press() {
  digitalWrite(PIN_SHUTTER, HIGH); digitalWrite(PIN_FOCUS, HIGH); digitalWrite(PIN_LED, HIGH);
}
static inline void shutter_open() { digitalWrite(PIN_SHUTTER, LOW); digitalWrite(PIN_LED, LOW); }
static inline void focus_set(uint8_t on) { digitalWrite(PIN_FOCUS, on ? HIGH : LOW); }
#endif

// Close SHUTTER+FOCUS now. Call with interrupts disabled (or from an ISR).
static inline void press_now(uint8_t src, uint8_t level) {
  lines_press();
  g_pressing = 1;
  g_lockout = 1;
  g_fire_src = src;
  g_fire_level = level;
  g_fire_us = micros();
  g_press_ms = millis();
  g_fired = 1;
}

static inline void on_sample(uint8_t s) {
  g_last = s;
  if (s > g_max) g_max = s;
  uint8_t above = s > g_trip;
  // Edge-triggered: a flash is a crossing from below to above the trip level,
  // so light that comes up and stays (sun, a lamp) can release at most once.
  if (above && !g_was_above && s_auto && g_trip_ok && !g_lockout) press_now('P', s);
  g_was_above = above;
  uint8_t b = g_base8;
  g_acc += s;
  g_devacc += (s > b) ? (uint8_t)(s - b) : (uint8_t)(b - s);
  if (++g_n == 16) {
    g_block_mean = g_acc >> 4;
    g_block_dev = g_devacc >> 4;
    g_acc = 0;
    g_devacc = 0;
    g_n = 0;
    g_block_ready = 1;
  }
}

#if FAST_PATH
ISR(ADC_vect) { on_sample(ADCH); }
#endif

static void adc_start() {
#if FAST_PATH
  ADMUX = _BV(REFS0) | _BV(ADLAR);           // AVcc reference, 8-bit left-adjusted, ADC0 (A0)
  DIDR0 = _BV(ADC0D);                         // no digital input buffer on A0
  ADCSRB = 0;                                 // free-running
  ADCSRA = _BV(ADEN) | _BV(ADSC) | _BV(ADATE) | _BV(ADIE) | _BV(ADPS2) | _BV(ADPS1);  // /64: ~19.2 kHz
#endif
}

// Baseline and trip level, updated once per 16-sample block (~1.2 kHz).
// Time constants at 1.2 kHz: warm-up 2^4 blocks, normal 2^10 (~0.85 s),
// lit 2^14 (~14 s) so a long flash is not learned but a lasting change is.
static inline void ema(uint32_t &acc, uint8_t x, uint8_t shift) {
  int32_t diff = ((int32_t)x << 16) - (int32_t)acc;
  acc += diff >> shift;
}

static void update_baseline() {
  uint8_t m, d;
  noInterrupts();
  if (!g_block_ready) { interrupts(); return; }
  m = g_block_mean;
  d = g_block_dev;
  g_block_ready = 0;
  interrupts();

  uint8_t base = base32 >> 16;
  uint16_t dev16 = dev32 >> 12;  // mean deviation x 16
  uint16_t thr = max((uint16_t)p_thr_min, (uint16_t)((p_k * (uint32_t)dev16 + 8) >> 4));

  if (blocks < 600) {           // ~0.5 s warm-up: converge fast, never fire
    if (blocks == 0) { base32 = (uint32_t)m << 16; dev32 = 2UL << 16; }
    ema(base32, m, 4);
    ema(dev32, d, 4);
    blocks++;
  } else if (!g_lockout) {
    bool lit = (uint16_t)m > (uint16_t)base + thr;
    ema(base32, m, lit ? 14 : 10);
    if (!lit) ema(dev32, d, 10);
  }

  base = base32 >> 16;
  dev16 = dev32 >> 12;
  thr = max((uint16_t)p_thr_min, (uint16_t)((p_k * (uint32_t)dev16 + 8) >> 4));
  uint16_t trip = (uint16_t)base + thr;
  g_base8 = base;
  if (blocks >= 600 && trip < 255) {
    g_trip = (uint8_t)trip;
    g_trip_ok = 1;
  } else {
    g_trip_ok = 0;  // sensor saturated (or warming up): shade it / smaller resistor
  }
}

static void host_fire() {
  noInterrupts();
  if (g_pressing) g_press_ms = millis();  // already pressed: extend the press
  else press_now('H', g_last);
  interrupts();
}

static void print_status() {
  Serial.print(F("S "));
  Serial.print(s_auto); Serial.print(' ');
  Serial.print(s_awake); Serial.print(' ');
  Serial.print(s_tele); Serial.print(' ');
  Serial.print(p_thr_min); Serial.print(' ');
  Serial.print(p_k); Serial.print(' ');
  Serial.print(p_hold_ms); Serial.print(' ');
  Serial.print(p_rearm_ms); Serial.print(' ');
  Serial.print((uint8_t)(base32 >> 16)); Serial.print(' ');
  Serial.println((uint16_t)(dev32 >> 12));
}

static char cmd[32];
static uint8_t cmdlen = 0;

static void parse_settings() {  // "S<thr>,<k>,<hold>,<rearm>"
  char *p = cmd + 1, *e;
  long v[4];
  for (uint8_t i = 0; i < 4; i++) {
    v[i] = strtol(p, &e, 10);
    if (e == p) { Serial.println(F("E bad S command")); return; }
    p = (*e == ',') ? e + 1 : e;
  }
  p_thr_min = constrain(v[0], 1, 250);
  p_k = constrain(v[1], 1, 40);
  p_hold_ms = constrain(v[2], 20, 30000);
  p_rearm_ms = constrain(v[3], 50, 60000);
  Serial.println(F("OK"));
}

static void handle_serial() {
  while (Serial.available()) {
    int c = Serial.read();
    if (cmdlen) {  // inside an S... line
      if (c == '\n' || c == '\r') { cmd[cmdlen] = 0; parse_settings(); cmdlen = 0; }
      else if (cmdlen < sizeof(cmd) - 1) cmd[cmdlen++] = (char)c;
      continue;
    }
    switch (c) {
      case 't': host_fire(); break;
      case 'A': s_auto = 1; break;
      case 'a': s_auto = 0; break;
      case 'W': s_awake = 1; noInterrupts(); focus_set(1); interrupts(); break;
      case 'w': s_awake = 0; noInterrupts(); if (!g_pressing) focus_set(0); interrupts(); break;
      case 'M': s_tele = 1; break;
      case 'm': s_tele = 0; break;
      case '?': print_status(); break;
      case 'S': cmd[0] = 'S'; cmdlen = 1; break;
      default: break;
    }
  }
}

void setup() {
  pinMode(PIN_SHUTTER, OUTPUT);
  pinMode(PIN_FOCUS, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_SHUTTER, LOW);
  digitalWrite(PIN_FOCUS, s_awake ? HIGH : LOW);
  digitalWrite(PIN_LED, LOW);
  Serial.begin(115200);
  adc_start();
  Serial.print(F("HELLO STORMTRIGGER "));
  Serial.print(PROTOCOL);
#if FAST_PATH
  Serial.println(F(" 328P"));
#else
  Serial.println(F(" GENERIC"));
#endif
}

void loop() {
#if !FAST_PATH
  noInterrupts();
  on_sample(analogRead(A0) >> 2);
  interrupts();
#endif
  handle_serial();
  update_baseline();

  uint32_t now = millis();
  if (g_pressing && (uint32_t)(now - g_press_ms) >= p_hold_ms) {
    noInterrupts();
    shutter_open();
    focus_set(s_awake);
    g_pressing = 0;
    interrupts();
    lockout_until = now + p_rearm_ms;
  }
  if (!g_pressing && g_lockout && (int32_t)(now - lockout_until) >= 0) g_lockout = 0;

  if (g_fired) {
    uint32_t us;
    uint8_t src, level;
    noInterrupts();
    us = g_fire_us; src = g_fire_src; level = g_fire_level; g_fired = 0;
    interrupts();
    Serial.print(F("F "));
    Serial.print(us); Serial.print(' ');
    Serial.print((char)src); Serial.print(' ');
    Serial.print(level); Serial.print(' ');
    Serial.println(g_trip_ok ? g_trip : 256);
  }

  if (s_tele && (uint32_t)(now - last_tele) >= 10) {
    last_tele = now;
    uint8_t mx;
    noInterrupts();
    mx = g_max;
    g_max = g_last;
    interrupts();
    Serial.print(F("T "));
    Serial.print(mx); Serial.print(' ');
    Serial.print((uint8_t)(base32 >> 16)); Serial.print(' ');
    Serial.println(g_trip_ok ? g_trip : 256);
  }
}
