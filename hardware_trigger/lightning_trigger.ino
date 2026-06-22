/*
  StormWatcher - Hardware lightning trigger for Nikon (MC-DC2)
  ============================================================

  The fast path. A photo sensor watches the sky and an Arduino shorts the
  camera's shutter line (through an optocoupler) the instant the light spikes -
  reaction in MICROSECONDS instead of the hundreds of milliseconds a
  PC-over-USB / phone-app trigger needs. No laptop, no live view, no software.

  It fires on a fast RISE in light (rate of change), not absolute brightness,
  so it works against a bright daytime sky as well as at night.

  ------------------------------------------------------------------ HARDWARE
  - Arduino Uno / Nano (any 5 V AVR board; 3.3 V boards: see README).

  - Photo sensor aimed at the sky (phototransistor, emitter-follower):

        +5V ----[ phototransistor ]----+----> A0
                                        |
                                      [ 10k ]
                                        |
                                       GND

  - PC817 optocoupler firing the camera's shutter, keeping the camera
    electrically isolated from the Arduino:

        D2 ---[ 330 ]---|>|---GND        (LED side: Arduino)
                       PC817
        cam SHUTTER ---( C  )            (output side: shorts the camera's
        cam GND     ---( E  )             shutter line to its own ground)

  - Camera link: easiest is to sacrifice a cheap wired MC-DC2 remote and use
    its three wires (GROUND / FOCUS / SHUTTER). Tie FOCUS + SHUTTER together so
    the camera half- and full-presses at once. Full wiring + how to identify
    the wires with a multimeter is in hardware_trigger/README.md.

  -------------------------------------------------------------- CAMERA SETUP
  - Manual focus, set to infinity (no AF hunting).
  - Manual (M) exposure with fixed ISO / shutter / aperture.
  - Drive mode: single, or continuous to rip a short burst per trigger and
    catch later return strokes.

  Honest limit: the camera's own shutter lag (~80-150 ms) means even this can't
  catch the very first return stroke - but lightning flashes have multiple
  strokes over ~100-500 ms plus continuing current, so a microsecond-fast
  trigger reliably lands the later strokes. That's exactly how commercial
  photodiode triggers (MIOPS, Pluto, etc.) work in daylight.
*/

const uint8_t  SENSOR_PIN  = A0;   // photo sensor input
const uint8_t  SHUTTER_PIN = 2;    // -> optocoupler LED -> camera shutter
const uint8_t  LED_PIN     = 13;   // on-board LED flashes on every trigger

// ------------------------------------------------------------------- TUNING
const int      THRESHOLD     = 40;     // rise (ADC counts) above ambient to fire
const uint16_t SHUTTER_HOLD  = 250;    // ms to hold the shutter line shorted
const uint16_t REARM_DELAY   = 400;    // ms to ignore light after firing
const float    BASELINE_DECAY = 0.001; // how fast the ambient baseline tracks

float baseline = 0;

void fire() {
  digitalWrite(SHUTTER_PIN, HIGH);   // optocoupler conducts -> shutter shorted
  digitalWrite(LED_PIN, HIGH);
  delay(SHUTTER_HOLD);
  digitalWrite(SHUTTER_PIN, LOW);
  digitalWrite(LED_PIN, LOW);
}

void setup() {
  pinMode(SHUTTER_PIN, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(SHUTTER_PIN, LOW);

  Serial.begin(115200);

  // seed the baseline with the current ambient light level
  long acc = 0;
  for (int i = 0; i < 64; i++) acc += analogRead(SENSOR_PIN);
  baseline = acc / 64.0;

  Serial.println(F("StormWatcher hardware trigger armed."));
  Serial.print(F("ambient baseline = "));
  Serial.println((int)baseline);
}

void loop() {
  int v = analogRead(SENSOR_PIN);

  // a fast rise above the slowly-tracked ambient level = a lightning flash
  if (v - baseline > THRESHOLD) {
    Serial.print(F("FIRE  v="));    Serial.print(v);
    Serial.print(F("  baseline=")); Serial.println((int)baseline);

    fire();

    // wait out the flash's after-glow, then re-seed ambient and re-arm
    delay(REARM_DELAY);
    baseline = analogRead(SENSOR_PIN);
    return;
  }

  // track ambient slowly so drifting sun/cloud light doesn't trip the trigger
  baseline += (v - baseline) * BASELINE_DECAY;
}
