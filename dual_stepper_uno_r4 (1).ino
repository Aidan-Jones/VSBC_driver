/*
  Ultra-precision synchronized dual stepper control
  Arduino Uno R4 (Minima or WiFi) -- uses hardware timer interrupts
  via FspTimer for jitter-free pulse generation.

  Wiring:
    STEP_PIN -> NPN transistor buffer -> BOTH drivers' PUL- (tied together)
    DIR_PIN  -> NPN transistor buffer -> BOTH drivers' DIR- (tied together)
    Both drivers' PUL+ and DIR+ -> +5V

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

FspTimer step_timer;

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

bool startTimer(float freq_hz) {
  uint8_t timer_type = GPT_TIMER;
  int8_t tindex = FspTimer::get_available_timer(timer_type);
  if (tindex < 0) {
    tindex = FspTimer::get_available_timer(timer_type, true);
  }
  if (tindex < 0) return false;

  step_timer.begin(TIMER_MODE_PERIODIC, timer_type, tindex, freq_hz, 50.0f, timer_callback);
  step_timer.setup_overflow_irq();
  step_timer.open();
  step_timer.start();
  return true;
}

void moveSteps(long steps, bool clockwise, float step_rate_hz) {
  digitalWrite(DIR_PIN, clockwise ? HIGH : LOW);
  delayMicroseconds(10);   // direction setup time before first pulse

  steps_remaining = steps;
  step_state = false;
  motion_active = true;

  // Timer toggles the pin twice per pulse (rising edge, then falling edge)
  startTimer(step_rate_hz * 2.0f);

  while (motion_active) {
    // ISR does all the work; just wait for the move to finish
  }

  step_timer.stop();
  step_timer.close();
}

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(STEP_PIN, LOW);
  digitalWrite(DIR_PIN, LOW);
}

void loop() {
  moveSteps(STEPS_PER_REV, true, 500);    // one revolution forward, 500 steps/sec
  delay(500);

  moveSteps(STEPS_PER_REV, false, 500);   // one revolution back
  delay(500);
}
