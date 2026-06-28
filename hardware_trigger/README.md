# StormWatcher — Hardware lightning trigger (Nikon MC-DC2)

The **fastest** way to fire the D3300 on lightning. A photo sensor + Arduino
short the camera's shutter line directly through the **MC-DC2** remote port, so
the camera fires within **microseconds** of the flash. No laptop, no live view,
no digiCamControl — none of the USB/app latency that makes a software trigger
too slow.

| Path | Detect → shutter latency |
|------|--------------------------|
| Phone/PC app over USB or Wi-Fi (e.g. MotionCam Pro), `nikon_lightning.py` | hundreds of ms |
| **This hardware trigger** | **microseconds** (+ the camera's own ~80–150 ms shutter lag, which nothing avoids) |

> **Reality check:** even at microsecond speed, the camera's mechanical shutter
> lag (~80–150 ms) means you still won't catch the *first* return stroke. But a
> lightning flash has multiple strokes spread over ~100–500 ms plus continuing
> current, so a trigger this fast reliably lands the later strokes. This is
> exactly how commercial triggers (MIOPS, Pluto Trigger) work in daylight.

---

## Parts

- **Arduino Uno or Nano** (any 5 V AVR board).
- **Phototransistor** (e.g. an infrared/visible phototransistor) — or a
  photodiode in photoconductive mode. Aimed at the sky.
- **PC817 optocoupler** (any common opto-isolator works).
- Resistors: **10 kΩ** (sensor load), **330 Ω** (optocoupler LED).
- A **cheap wired MC-DC2 remote shutter release** to sacrifice for its plug +
  3 wires. (Buying the right plug alone is harder than cutting up a $5 remote.)
- Optional: **100 Ω** resistor in series with the camera shutter line (mild
  extra safety), a small project box, and a potentiometer if you want a
  physical sensitivity knob.

The MC-DC2 connector fits: D3300, D3x00/D5x00/D7x00 series, D610/D750, etc.

---

## Wiring

### 1. Photo sensor → A0
Emitter-follower: a fast rise in light raises the voltage on A0.
```
  +5V ──[ phototransistor C→E ]──┬── A0
                                 │
                               [ 10k ]
                                 │
                                GND
```
Point the phototransistor at the part of the sky you're shooting. Shade it from
direct sun hitting the lens of the sensor if it pins to max.

### 2. Arduino → optocoupler → camera shutter
The optocoupler keeps the camera fully isolated from the Arduino — the Arduino
just turns on an LED inside the PC817, and the PC817's output transistor shorts
the camera's shutter line to the camera's ground, which is all a remote does.
```
  D2 ──[ 330 ]──▶|── GND          (PC817 pins 1–2, the LED)
                PC817
  cam SHUTTER ──( collector )     (PC817 pin 4)   ← optional 100Ω in series
  cam GND     ──( emitter )       (PC817 pin 3)
```
Orientation matters: the PC817 **collector** goes to the camera's shutter wire
(the side the camera holds positive), the **emitter** to the camera ground.

### 3. Identify the MC-DC2 wires (no pinout guessing)
Cut the cable off the wired remote. It has 3 wires: **GROUND, FOCUS, SHUTTER**.
Find them with a multimeter in continuity mode, plug in the remote, and:
- Press the button **half-way** → the wire that beeps against ground is **FOCUS**.
- Press **fully** → the additional wire that beeps against ground is **SHUTTER**.
- The common wire is **GROUND**.

Then wire:
- **GROUND** → PC817 emitter (and your camera-side ground).
- **SHUTTER** → PC817 collector.
- **FOCUS** → tie to SHUTTER so the camera half- and full-presses together
  (instant release in manual-focus mode).

---

## Camera setup (matters for speed too)
- **Manual focus**, focused to **infinity** — no AF hunting.
- **Manual (M) exposure**, fixed ISO / shutter / aperture.
- **Drive mode:** single, or **continuous** so each trigger rips a short burst
  and catches later strokes (the `SHUTTER_HOLD` of 250 ms holds the "button"
  down long enough for a few frames).

---

## Upload & calibrate
1. Open `lightning_trigger.ino` in the Arduino IDE, select your board/port,
   **Upload**.
2. Open **Serial Monitor** at **115200 baud**. It prints the ambient baseline,
   and a `FIRE v=… baseline=…` line every time it triggers.
3. **Set the threshold:** point the sensor at the sky and watch that nothing
   fires on normal brightness changes (clouds, sun). Test-flash it with a camera
   flash or a fast phone-torch flick — it should fire. If it fires on nothing,
   raise `THRESHOLD`; if it ignores flashes, lower it.

### Tuning knobs (top of the sketch)
- `THRESHOLD` — ADC-count rise above ambient needed to fire. Lower = more
  sensitive.
- `SHUTTER_HOLD` — how long the shutter line stays shorted (250 ms). Longer in
  continuous drive = more frames per flash.
- `REARM_DELAY` — dead time after a fire so the flash's after-glow doesn't
  re-trigger it.
- `BASELINE_DECAY` — how fast it adapts to ambient light. Higher tracks faster
  (good for moving clouds) but can start to ignore slow flashes.

---

## How it works
The sketch keeps a slowly-tracked **baseline** of ambient light and fires when
the live reading jumps `THRESHOLD` counts **above** that baseline — i.e. it
detects the *rate of rise*, not the absolute level. That's why it works in
daylight: the bright sky just sets a high baseline, and only the sudden flash
spike crosses the threshold.

## Want it even faster?
- Use the AVR **analog comparator interrupt** (AIN0 vs a reference) to fire from
  an ISR in ~1 µs instead of polling `analogRead` (~100 µs). Trade-off: a fixed
  reference instead of the adaptive baseline above.
- A **photodiode + transimpedance op-amp** front end responds faster and cleaner
  than a bare phototransistor.

For most storms the polling sketch here is already orders of magnitude faster
than any app-based trigger.
