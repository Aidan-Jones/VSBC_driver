/*
  LEGACY SKETCH -- kept for reference only.
  Superseded by firmware/vsbc_firmware, which adds the load cell, safety stops,
  calibration stored in EEPROM, and the protocol the Raspberry Pi app uses.
  Its single-driver bring-up is also there: MOTORS A / MOTORS B, then M 5 / M -5
  for one revolution forward / back.

  This sketch still runs on its own from the Arduino IDE (Serial Monitor,
  115200 baud), but it has no load cell and no load or travel-limit stops, and
  it is not kept in sync with the new firmware. The Pi app will not connect to
  it: upload firmware/vsbc_firmware again before using the app.
  serial_logger.py, mentioned below, was removed (git history, commit cccd861).
*/

/*
  Single-driver bring-up and displacement-trial test
  Arduino Mega 2560 -- pulses ONE DM542T V4.0 driver at a time, chosen from
  the Serial Monitor, so each driver/motor/ball screw can be checked on its own.

  Uses the same wiring, driver settings and Timer1 step engine as
  dual_stepper_mega.ino (direct, common-cathode -- no transistors needed):
    D22 (PA0) STEP_A -> Driver A PUL+      Driver A PUL- -> Mega GND
    D23 (PA1) STEP_B -> Driver B PUL+      Driver B PUL- -> Mega GND
    D24 (PA2) DIR_A  -> Driver A DIR+      Driver A DIR- -> Mega GND
    D25 (PA3) DIR_B  -> Driver B DIR+      Driver B DIR- -> Mega GND
    ENA+/ENA- left unconnected (drivers enabled)
  With only one driver connected, wire it as driver A: D22, D24 and GND.
  The driver that is not selected gets no pulses; its STEP and DIR stay LOW.

  Ball screws: 5 mm lead, direct coupled -> 0.0125 mm per step at 400 steps/rev.

  Serial Monitor: 115200 baud. Commands:
    a / b      select driver A or B
    f / r      one revolution forward / reverse
    t          back-and-forth test (1 rev fwd, 1 rev back) x3
    s<number>  set step rate for f/r/t in steps/s, e.g. s500 (max 800)
    v<mm/s>    set trial displacement rate, e.g. v0.5
    d<mm>      set trial distance, e.g. d20
    o / c      run trial: open / close by the set distance at the set rate
    h          return the selected driver to its zero position
    z          set the selected driver's current position as zero
    ?          show menu and current settings
  Send any key (e.g. x + Enter) during a move to cancel it immediately.
*/

#include <util/atomic.h>

// Printed at startup so serial_logger.py can name the log file after the sketch.
const char SKETCH_NAME[] = "single_driver_test";

// Port A bit masks (Mega pins 22-25)
const uint8_t STEP_A = _BV(PA0);   // D22
const uint8_t STEP_B = _BV(PA1);   // D23
const uint8_t DIR_A  = _BV(PA2);   // D24
const uint8_t DIR_B  = _BV(PA3);   // D25

const uint8_t ALL_MASK = STEP_A | STEP_B | DIR_A | DIR_B;

// Must match the microstep DIP switch setting on the driver under test.
const long STEPS_PER_REV = 400;

// Ball screw lead: linear travel per motor revolution (direct coupling).
const float SCREW_LEAD_MM = 5.0f;
const float MM_PER_STEP   = SCREW_LEAD_MM / STEPS_PER_REV;   // 0.0125 mm

// Default step rate for f/r/t, in steps/sec (500 at 400 pulses/rev = 75 RPM).
const float DEFAULT_STEP_RATE_HZ = 500.0f;

// Motor speed limit -- see dual_stepper_mega.ino for the derivation. Moves
// start at full rate with no ramp, so this applies to both s and v.
const float MAX_MOTOR_RPM = 120.0f;
const float MAX_MOTOR_STEP_RATE_HZ = MAX_MOTOR_RPM / 60.0f * STEPS_PER_REV;  // 800 steps/s

// Trial defaults and limits -- keep in step with dual_stepper_mega.ino.
const float DEFAULT_RATE_MM_S   = 0.5f;
const float DEFAULT_DISTANCE_MM = 10.0f;
const float MIN_RATE_MM_S   = 0.01f;                                  // 0.8 steps/s
const float MAX_RATE_MM_S   = MAX_MOTOR_RPM / 60.0f * SCREW_LEAD_MM;  // 10 mm/s = 800 steps/s
const float MAX_DISTANCE_MM = 100.0f;   // TODO: set to the real beam travel

// DIR level that makes the beam open. Flip if "open" moves the wrong way.
const bool OPEN_DIR_HIGH = true;
// Set true if motor B is mounted mirrored and turns the wrong way.
const bool INVERT_B = false;

// How often to print position during a move.
const unsigned long PROGRESS_INTERVAL_MS = 500;

// Upper limit for the ISR-driven step rate.
const float MAX_STEP_RATE_HZ = 20000.0f;

const int TEST_CYCLES = 3;

char selected_driver = 'A';
uint8_t step_mask = STEP_A;        // STEP bit of the selected driver only
uint8_t dir_mask  = DIR_A;
float step_rate_hz = DEFAULT_STEP_RATE_HZ;

float rate_mm_s   = DEFAULT_RATE_MM_S;
float distance_mm = DEFAULT_DISTANCE_MM;
long position_steps[2] = {0, 0};   // per driver (A, B), + = open

float current_rate_hz = 0.0f;  // requested step rate the timer is set for
float actual_rate_hz  = 0.0f;  // rate the timer really produces (after rounding)
uint8_t timer_cs_bits = 0;     // Timer1 clock-select bits for the current rate

volatile bool step_state = false;
volatile long steps_remaining = 0;
volatile bool motion_active = false;
volatile uint8_t isr_step_mask = STEP_A;

enum MoveResult { MOVE_DONE, MOVE_CANCELLED, MOVE_TIMEOUT, MOVE_ERROR };

void stopTimer() {
  TCCR1B = _BV(WGM12);         // CTC mode, clock stopped
  TIMSK1 &= ~_BV(OCIE1A);
}

ISR(TIMER1_COMPA_vect) {
  if (!motion_active) return;

  step_state = !step_state;
  PORTA ^= isr_step_mask;      // only the selected driver's STEP line

  if (!step_state) {           // just completed the falling edge = one full pulse
    steps_remaining--;
    if (steps_remaining <= 0) {
      motion_active = false;
      PORTA &= ~isr_step_mask;
      stopTimer();
    }
  }
}

// Timer1 CTC compare rate = 2x step rate (rising + falling edge per pulse).
// Picks the smallest prescaler that fits in 16 bits. Only call while stopped.
bool setStepRate(float rate_hz) {
  if (rate_hz <= 0.0f || rate_hz > MAX_STEP_RATE_HZ) return false;

  const uint16_t prescalers[] = {1, 8, 64, 256, 1024};
  const uint8_t  cs_bits[]    = {_BV(CS10), _BV(CS11), _BV(CS11) | _BV(CS10),
                                 _BV(CS12), _BV(CS12) | _BV(CS10)};

  for (uint8_t i = 0; i < 5; i++) {
    uint32_t ticks = (uint32_t)(F_CPU / (2.0f * prescalers[i] * rate_hz) + 0.5f);
    if (ticks >= 1 && ticks <= 65536UL) {
      OCR1A = ticks - 1;
      timer_cs_bits = cs_bits[i];
      current_rate_hz = rate_hz;
      actual_rate_hz = F_CPU / (2.0f * prescalers[i] * ticks);
      return true;
    }
  }
  return false;
}

int driverIndex() {
  return (selected_driver == 'A') ? 0 : 1;
}

// DIR level that opens the beam on the selected driver.
bool openDirHigh() {
  return OPEN_DIR_HIGH != (selected_driver == 'B' && INVERT_B);
}

// Set the selected driver's DIR line and start the timer. Returns immediately;
// the ISR does the stepping. Call waitForMove() afterwards.
bool startMove(long steps, bool dir_high, float rate_hz) {
  if (steps <= 0) return false;

  if (rate_hz != current_rate_hz) {
    if (!setStepRate(rate_hz)) {
      Serial.println("ERROR: could not set step rate");
      return false;
    }
  }

  // Timer is stopped here, so it is safe to touch PORTA and the multi-byte counter.
  PORTA &= ~ALL_MASK;          // other driver fully idle
  if (dir_high) PORTA |= dir_mask;
  delayMicroseconds(10);   // direction setup time before first pulse (DM542T V4.0 min 5 us)

  isr_step_mask = step_mask;
  steps_remaining = steps;
  step_state = false;
  motion_active = true;

  TCNT1 = 0;
  TIFR1 = _BV(OCF1A);                  // clear any stale compare flag
  TIMSK1 |= _BV(OCIE1A);
  TCCR1B = _BV(WGM12) | timer_cs_bits; // start the clock
  return true;
}

// Stop the timer and drop the STEP line. Returns true if a pulse was cut
// short after its rising edge (the driver still counts that step).
bool stopMotion() {
  stopTimer();
  motion_active = false;
  bool half_pulse = step_state;
  step_state = false;
  PORTA &= ~step_mask;
  return half_pulse;
}

long remainingSteps() {
  long remaining;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { remaining = steps_remaining; }
  return remaining;
}

// Discard everything in the serial input buffer, including characters still
// arriving (e.g. the rest of "stop" + newline after a cancel).
void drainSerial() {
  unsigned long last_ms = millis();
  while (millis() - last_ms < 50) {
    if (Serial.available()) {
      Serial.read();
      last_ms = millis();
    }
  }
}

// Any non-whitespace character cancels. Whitespace is ignored so the newline
// that follows the command which started the move does not cancel it.
bool cancelRequested() {
  while (Serial.available()) {
    if (!isspace(Serial.read())) return true;
  }
  return false;
}

void printPosition(long pos_steps) {
  Serial.print(pos_steps * MM_PER_STEP, 3);
  Serial.print(" mm");
}

// Start a move and wait for it, printing progress (if requested) and watching
// for a cancel key. Updates the selected driver's position by the steps
// actually made; *completed gets that count.
MoveResult runMove(long steps, bool dir_high, float rate_hz, bool report, long *completed) {
  *completed = 0;
  while (Serial.available()) Serial.read();   // leftovers from the command line
  if (!startMove(steps, dir_high, rate_hz)) return MOVE_ERROR;

  unsigned long timeout_ms = (unsigned long)(steps * 1000.0f / rate_hz) + 1000;
  unsigned long start_ms = millis();
  unsigned long report_ms = start_ms;
  int sign = (dir_high == openDirHigh()) ? 1 : -1;
  long start_pos = position_steps[driverIndex()];
  MoveResult result = MOVE_DONE;

  while (motion_active) {
    if (cancelRequested()) {
      result = MOVE_CANCELLED;
      break;
    }
    unsigned long now_ms = millis();
    if (now_ms - start_ms > timeout_ms) {
      result = MOVE_TIMEOUT;
      break;
    }
    if (report && now_ms - report_ms >= PROGRESS_INTERVAL_MS) {
      report_ms += PROGRESS_INTERVAL_MS;
      Serial.print("  t = ");
      Serial.print((now_ms - start_ms) / 1000.0f, 1);
      Serial.print(" s   position = ");
      printPosition(start_pos + sign * (steps - remainingSteps()));
      Serial.println();
    }
  }

  bool half_pulse = stopMotion();
  *completed = steps - remainingSteps() + (half_pulse ? 1 : 0);
  position_steps[driverIndex()] = start_pos + sign * *completed;

  if (result == MOVE_CANCELLED) {
    drainSerial();
    Serial.println();
    Serial.print("CANCELLED. ");
  } else if (result == MOVE_TIMEOUT) {
    Serial.println();
    Serial.print("ERROR: move timed out. ");
  }
  return result;
}

// Move one revolution and report how long it took. Returns false if the move
// did not finish (cancelled, timed out or failed).
bool revolution(bool clockwise) {
  Serial.print("Driver ");
  Serial.print(selected_driver);
  Serial.print(clockwise ? ": forward " : ": reverse ");
  Serial.print(STEPS_PER_REV);
  Serial.print(" steps @ ");
  Serial.print(step_rate_hz, 1);
  Serial.print(" steps/s ... ");

  unsigned long start_us = micros();
  long completed;
  MoveResult result = runMove(STEPS_PER_REV, clockwise, step_rate_hz, false, &completed);
  unsigned long elapsed_us = micros() - start_us;

  if (result == MOVE_DONE) {
    Serial.print("done in ");
    Serial.print(elapsed_us / 1000.0f, 1);
    Serial.println(" ms");
  } else if (result != MOVE_ERROR) {
    Serial.print(completed);
    Serial.println(" steps made.");
  }
  return result == MOVE_DONE;
}

// Move the selected driver's screw `steps` in one direction at the set
// displacement rate.
void runTrial(long steps, bool opening) {
  if (steps <= 0) {
    Serial.println("Nothing to move.");
    return;
  }

  float rate_hz = rate_mm_s / MM_PER_STEP;
  if (!setStepRate(rate_hz)) {
    Serial.println("ERROR: could not set step rate");
    return;
  }

  Serial.println();
  Serial.print("Driver ");
  Serial.print(selected_driver);
  Serial.print(opening ? ": OPEN " : ": CLOSE ");
  Serial.print(steps * MM_PER_STEP, 3);
  Serial.print(" mm (");
  Serial.print(steps);
  Serial.print(" steps) at ");
  Serial.print(actual_rate_hz * MM_PER_STEP, 4);
  Serial.print(" mm/s (");
  Serial.print(actual_rate_hz, 2);
  Serial.print(" steps/s), expected ");
  Serial.print(steps / actual_rate_hz, 1);
  Serial.println(" s");
  Serial.println("Send any key to cancel.");

  bool dir_high = (opening == openDirHigh());
  unsigned long start_ms = millis();
  long completed;
  MoveResult result = runMove(steps, dir_high, rate_hz, true, &completed);
  if (result == MOVE_ERROR) return;
  float elapsed_s = (millis() - start_ms) / 1000.0f;

  if (result == MOVE_DONE) Serial.print("Done: ");
  Serial.print("moved ");
  Serial.print(completed * MM_PER_STEP, 3);
  Serial.print(" mm in ");
  Serial.print(elapsed_s, 2);
  Serial.print(" s (avg ");
  Serial.print(elapsed_s > 0.0f ? completed * MM_PER_STEP / elapsed_s : 0.0f, 4);
  Serial.print(" mm/s). Position: ");
  printPosition(position_steps[driverIndex()]);
  Serial.println();
}

void selectDriver(char driver) {
  selected_driver = driver;
  step_mask = (driver == 'A') ? STEP_A : STEP_B;
  dir_mask  = (driver == 'A') ? DIR_A  : DIR_B;
  Serial.print("Selected driver ");
  Serial.print(selected_driver);
  Serial.print("   Position: ");
  printPosition(position_steps[driverIndex()]);
  Serial.println();
}

void printMenu() {
  Serial.println();
  Serial.println("=== Single-driver test (Mega 2560) ===");
  Serial.print("Sketch: ");
  Serial.println(SKETCH_NAME);
  Serial.println("  a / b      select driver A (D22/D24) or B (D23/D25)");
  Serial.println("  f / r      one revolution forward / reverse");
  Serial.println("  t          back-and-forth test x3");
  Serial.print("  s<number>  set f/r/t step rate in steps/s (e.g. s500, max ");
  Serial.print(MAX_MOTOR_STEP_RATE_HZ, 0);
  Serial.println(")");
  Serial.print("  v<mm/s>    set trial displacement rate (e.g. v0.5, max ");
  Serial.print(MAX_RATE_MM_S, 1);
  Serial.println(")");
  Serial.println("  d<mm>      set trial distance (e.g. d20)");
  Serial.println("  o / c      run trial: open / close by distance at rate");
  Serial.println("  h          return selected driver to zero position");
  Serial.println("  z          set selected driver's position as zero");
  Serial.println("  ?          show this menu");
  Serial.println("  Any key during a move cancels it.");
  Serial.print("Driver: ");
  Serial.print(selected_driver);
  Serial.print("   Rev rate: ");
  Serial.print(step_rate_hz, 1);
  Serial.print(" steps/s (");
  Serial.print(step_rate_hz * 60.0f / STEPS_PER_REV, 1);
  Serial.print(" RPM)   Steps/rev: ");
  Serial.println(STEPS_PER_REV);
  Serial.print("Trial rate: ");
  Serial.print(rate_mm_s, 4);
  Serial.print(" mm/s (");
  Serial.print(rate_mm_s / MM_PER_STEP, 2);
  Serial.print(" steps/s)   Distance: ");
  Serial.print(distance_mm, 3);
  Serial.print(" mm   Position: ");
  printPosition(position_steps[driverIndex()]);
  Serial.println();
}

void setup() {
  DDRA  |= ALL_MASK;           // D22-D25 as outputs
  PORTA &= ~ALL_MASK;          // all LOW

  TCCR1A = 0;
  stopTimer();
  TCNT1 = 0;
  setStepRate(step_rate_hz);

  Serial.begin(115200);
  printMenu();
}

void loop() {
  if (!Serial.available()) return;

  char c = Serial.read();
  switch (c) {
    case 'a': case 'A': selectDriver('A'); break;
    case 'b': case 'B': selectDriver('B'); break;
    case 'f': case 'F': revolution(true);  break;
    case 'r': case 'R': revolution(false); break;

    case 't': case 'T':
      for (int i = 1; i <= TEST_CYCLES; i++) {
        Serial.print("Cycle ");
        Serial.print(i);
        Serial.print("/");
        Serial.println(TEST_CYCLES);
        if (!revolution(true)) break;
        delay(500);
        if (!revolution(false)) break;
        delay(500);
      }
      break;

    case 's': case 'S': {
      float rate = Serial.parseFloat();
      if (rate > MAX_MOTOR_STEP_RATE_HZ) {
        Serial.print("ERROR: ");
        Serial.print(rate, 1);
        Serial.print(" steps/s exceeds the motor limit of ");
        Serial.print(MAX_MOTOR_STEP_RATE_HZ, 0);
        Serial.print(" steps/s (");
        Serial.print(MAX_MOTOR_RPM, 0);
        Serial.println(" RPM). Rate unchanged.");
      } else if (rate > 0.0f && setStepRate(rate)) {
        step_rate_hz = rate;
        Serial.print("Step rate set to ");
        Serial.print(step_rate_hz, 1);
        Serial.print(" steps/s (");
        Serial.print(step_rate_hz * 60.0f / STEPS_PER_REV, 1);
        Serial.println(" RPM)");
      } else {
        Serial.println("ERROR: step rate must be at least 0.12 steps/s. Rate unchanged.");
        setStepRate(step_rate_hz);
      }
      break;
    }

    case 'v': case 'V': {
      float rate = Serial.parseFloat();
      if (rate > MAX_RATE_MM_S) {
        Serial.print("ERROR: ");
        Serial.print(rate, 4);
        Serial.print(" mm/s exceeds the motor limit of ");
        Serial.print(MAX_RATE_MM_S, 2);
        Serial.print(" mm/s (");
        Serial.print(MAX_MOTOR_RPM, 0);
        Serial.println(" RPM). Rate unchanged.");
      } else if (rate < MIN_RATE_MM_S) {
        Serial.print("ERROR: rate must be at least ");
        Serial.print(MIN_RATE_MM_S, 2);
        Serial.println(" mm/s. Rate unchanged.");
      } else {
        rate_mm_s = rate;
        setStepRate(rate_mm_s / MM_PER_STEP);
        Serial.print("Trial rate set to ");
        Serial.print(rate_mm_s, 4);
        Serial.print(" mm/s (actual ");
        Serial.print(actual_rate_hz * MM_PER_STEP, 4);
        Serial.println(" mm/s)");
      }
      break;
    }

    case 'd': case 'D': {
      float dist = Serial.parseFloat();
      if (dist >= MM_PER_STEP && dist <= MAX_DISTANCE_MM) {
        distance_mm = dist;
        Serial.print("Distance set to ");
        Serial.print(distance_mm, 3);
        Serial.print(" mm (");
        Serial.print(lround(distance_mm / MM_PER_STEP));
        Serial.println(" steps)");
      } else {
        Serial.print("Invalid distance; must be between ");
        Serial.print(MM_PER_STEP, 4);
        Serial.print(" and ");
        Serial.print(MAX_DISTANCE_MM, 1);
        Serial.println(" mm");
      }
      break;
    }

    case 'o': case 'O': runTrial(lround(distance_mm / MM_PER_STEP), true);  break;
    case 'c': case 'C': runTrial(lround(distance_mm / MM_PER_STEP), false); break;

    case 'h': case 'H': {
      long pos = position_steps[driverIndex()];
      runTrial(labs(pos), pos < 0);
      break;
    }

    case 'z': case 'Z':
      position_steps[driverIndex()] = 0;
      Serial.print("Driver ");
      Serial.print(selected_driver);
      Serial.println(" position set to zero.");
      break;

    case '?': printMenu(); break;

    default: break;            // ignore newlines, spaces, etc.
  }
}
