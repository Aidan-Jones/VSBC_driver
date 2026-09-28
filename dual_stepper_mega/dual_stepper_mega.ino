/*
  Ultra-precision synchronized dual stepper control
  Arduino Uno R4 (Minima or WiFi) -- uses hardware timer interrupts
  via FspTimer for jitter-free pulse generation.

  Drivers: 2x Stepperonline DM542T V4.0 (one per motor)
  Motors:  2x Stepperonline 23HS30-3004S (1.8 deg, 3.0 A)

  Driver settings (must be IDENTICAL on both drivers):
    S2 selector: 5V   (factory default is 24V -- must be changed for 5V signals)
    SW1 OFF, SW2 OFF, SW3 ON  -> 2.37 A peak / 1.69 A RMS
    SW4 ON                    -> standstill current 90%
    SW5 OFF, SW6 ON, SW7 ON, SW8 ON -> 400 pulses/rev (matches STEPS_PER_REV)

  Wiring:
    STEP_PIN -> NPN transistor buffer -> BOTH drivers' PUL- (tied together)
    DIR_PIN  -> NPN transistor buffer -> BOTH drivers' DIR- (tied together)
    Both drivers' PUL+ and DIR+ -> +5V
    ENA+/ENA- left unconnected (drivers enabled)

  Because both drivers receive the exact same electrical pulse edge at the
  same instant, there is no possibility of relative timing skew between the
  two motors -- sync is guaranteed by the wiring, not the software.
  The hardware timer just makes sure each edge itself lands on schedule.
*/

#include "FspTimer.h"

const int STEP_PIN = 4;
const int DIR_PIN  = 5;

// Must match the microstep DIP switch setting on BOTH drivers -- keep identical.
const long STEPS_PER_REV = 400;

// Step rate used by the demo loop, in steps/sec (500 at 400 pulses/rev = 75 RPM).
const float STEP_RATE_HZ = 500.0f;

FspTimer step_timer;
float current_rate_hz = 0.0f;

volatile bool step_state = false;
volatile long steps_remaining = 0;
volatile bool motion_active = false;

void timer_callback(timer_callback_args_t *arg) {
  if (!motion_active) return;

  step_state = !step_state;
  digitalWrite(STEP_PIN, step_state);

  if (!step_state) {           // just completed the falling edge = one full pulse
    steps_remaining--;
    if (steps_remaining <= 0) {
      motion_active = false;
      digitalWrite(STEP_PIN, LOW);
    }
  }
}

// Set up the timer ONCE. FspTimer::close() does not release the timer channel
// and setup_overflow_irq() registers a new IRQ on every call, so re-creating
// the timer for each move eventually runs out of timers and hangs.
bool initTimer(float step_rate_hz) {
  uint8_t timer_type = GPT_TIMER;
  int8_t tindex = FspTimer::get_available_timer(timer_type);
  if (tindex < 0) {
    tindex = FspTimer::get_available_timer(timer_type, true);
  }
  if (tindex < 0) return false;

  // Timer toggles the pin twice per pulse (rising edge, then falling edge)
  if (!step_timer.begin(TIMER_MODE_PERIODIC, timer_type, tindex, step_rate_hz * 2.0f, 50.0f, timer_callback)) return false;
  if (!step_timer.setup_overflow_irq()) return false;
  if (!step_timer.open()) return false;

  current_rate_hz = step_rate_hz;
  return true;
}

// Stop here and blink the LED so a setup failure is obvious instead of a silent hang.
void fatalError(const char *msg) {
  Serial.println(msg);
  pinMode(LED_BUILTIN, OUTPUT);
  while (true) {
    digitalWrite(LED_BUILTIN, HIGH);
    delay(100);
    digitalWrite(LED_BUILTIN, LOW);
    delay(100);
  }
}

bool moveSteps(long steps, bool clockwise, float step_rate_hz) {
  if (steps <= 0 || step_rate_hz <= 0.0f) return true;

  if (step_rate_hz != current_rate_hz) {
    if (!step_timer.set_frequency(step_rate_hz * 2.0f)) {
      Serial.println("ERROR: could not set step rate");
      return false;
    }
    current_rate_hz = step_rate_hz;
  }

  digitalWrite(DIR_PIN, clockwise ? HIGH : LOW);
  delayMicroseconds(10);   // direction setup time before first pulse (DM542T V4.0 min 5 us)

  steps_remaining = steps;
  step_state = false;
  motion_active = true;

  step_timer.start();

  // ISR does all the work; wait for the move to finish, with a timeout safeguard
  unsigned long timeout_ms = (unsigned long)(steps * 1000.0f / step_rate_hz) + 1000;
  unsigned long start_ms = millis();
  while (motion_active) {
    if (millis() - start_ms > timeout_ms) {
      motion_active = false;
      step_timer.stop();
      digitalWrite(STEP_PIN, LOW);
      Serial.println("ERROR: move timed out");
      return false;
    }
  }

  step_timer.stop();
  return true;
}

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(STEP_PIN, LOW);
  digitalWrite(DIR_PIN, LOW);

  Serial.begin(115200);

  if (!initTimer(STEP_RATE_HZ)) {
    fatalError("ERROR: no hardware timer available");
  }
}

void loop() {
  moveSteps(STEPS_PER_REV, true, STEP_RATE_HZ);    // one revolution forward
  delay(500);

  moveSteps(STEPS_PER_REV, false, STEP_RATE_HZ);   // one revolution back
  delay(500);
}
