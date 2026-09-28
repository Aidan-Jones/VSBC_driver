/*
  Constant-rate displacement trials -- synchronized dual stepper control
  Arduino Mega 2560 (ATmega2560, 16 MHz) -- uses the 16-bit Timer1 compare
  interrupt for jitter-free pulse generation.

  Drivers:     2x Stepperonline DM542T V4.0 (one per motor)
  Motors:      2x Stepperonline 23HS30-3004S (1.8 deg, 3.0 A)
  Ball screws: 5 mm lead, coupled directly to the motors
               -> 400 steps/rev / 5 mm = 0.0125 mm per step

  Both ball screws move the beam together: every step pulse goes to both
  drivers on the same clock cycle. A trial moves a set distance at a constant
  displacement rate. The step rate is fixed by the hardware timer, so the rate
  stays constant whatever the load, as long as the motors do not stall (open
  loop -- there is no position feedback).

  Driver settings (must be IDENTICAL on both drivers):
    S2 selector: 5V   (factory default is 24V -- must be changed for 5V signals)
    SW1 OFF, SW2 OFF, SW3 ON  -> 2.37 A peak / 1.69 A RMS
    SW4 ON                    -> standstill current 90%
    SW5 OFF, SW6 ON, SW7 ON, SW8 ON -> 400 pulses/rev (matches STEPS_PER_REV)

  Wiring (direct, common-cathode -- no transistors needed):
    D22 (PA0) STEP_A -> Driver A PUL+      Driver A PUL- -> Mega GND
    D23 (PA1) STEP_B -> Driver B PUL+      Driver B PUL- -> Mega GND
    D24 (PA2) DIR_A  -> Driver A DIR+      Driver A DIR- -> Mega GND
    D25 (PA3) DIR_B  -> Driver B DIR+      Driver B DIR- -> Mega GND
    ENA+/ENA- left unconnected (drivers enabled)
  Each Mega pin sources the ~7-10 mA opto current directly (pin HIGH = signal on).

  All four signals are on the same AVR port (PORTA), so the ISR toggles
  both STEP lines with a single register write. Both drivers receive their
  pulse edge at the same instant -- there is no relative timing skew between
  the two motors.

  Serial Monitor: 115200 baud, any line ending. Commands:
    v<mm/s>    set displacement rate, e.g. v0.5
    d<mm>      set trial distance, e.g. d20
    o          run trial: open by the set distance at the set rate
    c          run trial: close by the set distance at the set rate
    h          return to the zero position at the set rate
    z          set the current position as zero
    ?          show menu and current settings
  Send any key (e.g. x + Enter) during a trial to cancel it immediately.
*/

#include <util/atomic.h>

// Printed at startup so serial_logger.py can name the log file after the sketch.
const char SKETCH_NAME[] = "dual_stepper_mega";

// Port A bit masks (Mega pins 22-25)
const uint8_t STEP_A = _BV(PA0);   // D22
const uint8_t STEP_B = _BV(PA1);   // D23
const uint8_t DIR_A  = _BV(PA2);   // D24
const uint8_t DIR_B  = _BV(PA3);   // D25

const uint8_t STEP_MASK = STEP_A | STEP_B;
const uint8_t DIR_MASK  = DIR_A | DIR_B;

// Must match the microstep DIP switch setting on BOTH drivers -- keep identical.
const long STEPS_PER_REV = 400;

// Ball screw lead: linear travel per motor revolution (direct coupling).
const float SCREW_LEAD_MM = 5.0f;
const float MM_PER_STEP   = SCREW_LEAD_MM / STEPS_PER_REV;   // 0.0125 mm

// Trial defaults, changeable from the Serial Monitor with v and d.
const float DEFAULT_RATE_MM_S   = 0.5f;
const float DEFAULT_DISTANCE_MM = 10.0f;

// Motor speed limit. Trials start at full rate with no ramp, and a stall breaks
// the constant-rate guarantee, so the motor must start and hold the rate from
// standstill under load. 23HS30-3004S: 1.89 N*m holding, 4.8 mH, 440 g*cm^2
// rotor. At the 2.37 A peak driver setting, torque starts to fall above about
// V / (2 * L * I) full steps/s: ~320 RPM on a 24 V supply, ~630 RPM on 48 V.
// 120 RPM stays well below that on any supply in the DM542T range and inside
// a safe no-ramp start rate, so close to full low-speed torque is available.
const float MAX_MOTOR_RPM = 120.0f;

// Rate and distance limits.
const float MIN_RATE_MM_S   = 0.01f;                                  // 0.8 steps/s
const float MAX_RATE_MM_S   = MAX_MOTOR_RPM / 60.0f * SCREW_LEAD_MM;  // 10 mm/s = 800 steps/s
const float MAX_DISTANCE_MM = 100.0f;   // TODO: set to the real beam travel

// DIR level that makes the beam open. Flip if "open" moves the wrong way.
const bool OPEN_DIR_HIGH = true;
// Set true if motor B is mounted mirrored and turns the wrong way.
const bool INVERT_B = false;

// How often to print position during a trial.
const unsigned long PROGRESS_INTERVAL_MS = 500;

// Upper limit for the ISR-driven step rate. The ISR toggles the pins twice per
// step, so much faster than this and the CPU spends all its time in the ISR.
const float MAX_STEP_RATE_HZ = 20000.0f;

float rate_mm_s   = DEFAULT_RATE_MM_S;
float distance_mm = DEFAULT_DISTANCE_MM;
long position_steps = 0;     // beam position relative to zero, + = open

float current_rate_hz = 0.0f;  // requested step rate the timer is set for
float actual_rate_hz  = 0.0f;  // rate the timer really produces (after rounding)
uint8_t timer_cs_bits = 0;     // Timer1 clock-select bits for the current rate

volatile bool step_state = false;
volatile long steps_remaining = 0;
volatile bool motion_active = false;

enum MoveResult { MOVE_DONE, MOVE_CANCELLED, MOVE_TIMEOUT };

void stopTimer() {
  TCCR1B = _BV(WGM12);         // CTC mode, clock stopped
  TIMSK1 &= ~_BV(OCIE1A);
}

ISR(TIMER1_COMPA_vect) {
  if (!motion_active) return;

  step_state = !step_state;
  PORTA ^= STEP_MASK;          // both STEP lines change on the same clock cycle

  if (!step_state) {           // just completed the falling edge = one full pulse
    steps_remaining--;
    if (steps_remaining <= 0) {
      motion_active = false;
      PORTA &= ~STEP_MASK;
      stopTimer();
    }
  }
}

// Timer1 in CTC mode interrupts every (OCR1A + 1) timer ticks. It toggles the
// pins twice per pulse (rising edge, then falling edge), so the compare rate
// is 2x the step rate. Picks the smallest prescaler that fits in 16 bits.
// Only call while the timer is stopped.
bool setStepRate(float step_rate_hz) {
  if (step_rate_hz <= 0.0f || step_rate_hz > MAX_STEP_RATE_HZ) return false;

  const uint16_t prescalers[] = {1, 8, 64, 256, 1024};
  const uint8_t  cs_bits[]    = {_BV(CS10), _BV(CS11), _BV(CS11) | _BV(CS10),
                                 _BV(CS12), _BV(CS12) | _BV(CS10)};

  for (uint8_t i = 0; i < 5; i++) {
    uint32_t ticks = (uint32_t)(F_CPU / (2.0f * prescalers[i] * step_rate_hz) + 0.5f);
    if (ticks >= 1 && ticks <= 65536UL) {
      OCR1A = ticks - 1;
      timer_cs_bits = cs_bits[i];
      current_rate_hz = step_rate_hz;
      actual_rate_hz = F_CPU / (2.0f * prescalers[i] * ticks);
      return true;
    }
  }
  return false;                // slower than ~0.12 steps/s
}

// Configure Timer1 once: CTC mode, stopped, interrupt disabled until a move.
bool initTimer(float step_rate_hz) {
  TCCR1A = 0;
  stopTimer();
  TCNT1 = 0;
  return setStepRate(step_rate_hz);
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

// Set both DIR lines and start the timer. Returns immediately; the ISR does
// the stepping. Call waitForMove() afterwards.
bool startMove(long steps, bool opening, float step_rate_hz) {
  if (steps <= 0) return false;

  if (step_rate_hz != current_rate_hz) {
    if (!setStepRate(step_rate_hz)) {
      Serial.println("ERROR: could not set step rate");
      return false;
    }
  }

  // Timer is stopped here, so it is safe to touch PORTA and the multi-byte counter.
  bool dir_a = (opening == OPEN_DIR_HIGH);
  bool dir_b = (dir_a != INVERT_B);
  uint8_t dir_bits = (dir_a ? DIR_A : 0) | (dir_b ? DIR_B : 0);
  PORTA = (PORTA & ~DIR_MASK) | dir_bits;
  delayMicroseconds(10);   // direction setup time before first pulse (DM542T V4.0 min 5 us)

  steps_remaining = steps;
  step_state = false;
  motion_active = true;

  TCNT1 = 0;
  TIFR1 = _BV(OCF1A);                  // clear any stale compare flag
  TIMSK1 |= _BV(OCIE1A);
  TCCR1B = _BV(WGM12) | timer_cs_bits; // start the clock
  return true;
}

// Stop the timer and drop both STEP lines. Returns true if a pulse was cut
// short after its rising edge (the drivers still count that step).
bool stopMotion() {
  stopTimer();
  motion_active = false;
  bool half_pulse = step_state;
  step_state = false;
  PORTA &= ~STEP_MASK;
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
// that follows the command which started the trial does not cancel it.
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

// Wait for the ISR to finish the move, printing progress and watching for a
// cancel key. *completed gets the number of steps actually made.
MoveResult waitForMove(long steps, bool opening, float step_rate_hz, long *completed) {
  unsigned long timeout_ms = (unsigned long)(steps * 1000.0f / step_rate_hz) + 1000;
  unsigned long start_ms = millis();
  unsigned long report_ms = start_ms;
  int sign = opening ? 1 : -1;
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
    if (now_ms - report_ms >= PROGRESS_INTERVAL_MS) {
      report_ms += PROGRESS_INTERVAL_MS;
      Serial.print("  t = ");
      Serial.print((now_ms - start_ms) / 1000.0f, 1);
      Serial.print(" s   position = ");
      printPosition(position_steps + sign * (steps - remainingSteps()));
      Serial.println();
    }
  }

  bool half_pulse = stopMotion();
  *completed = steps - remainingSteps() + (half_pulse ? 1 : 0);
  return result;
}

// Move both screws `steps` in one direction at the set displacement rate.
void runTrial(long steps, bool opening) {
  if (steps <= 0) {
    Serial.println("Nothing to move.");
    return;
  }

  float step_rate_hz = rate_mm_s / MM_PER_STEP;
  if (!setStepRate(step_rate_hz)) {
    Serial.println("ERROR: could not set step rate");
    return;
  }

  Serial.println();
  Serial.print(opening ? "OPEN " : "CLOSE ");
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

  while (Serial.available()) Serial.read();   // leftovers from the command line

  unsigned long start_ms = millis();
  if (!startMove(steps, opening, step_rate_hz)) return;

  long completed = 0;
  MoveResult result = waitForMove(steps, opening, step_rate_hz, &completed);
  float elapsed_s = (millis() - start_ms) / 1000.0f;
  position_steps += opening ? completed : -completed;

  if (result == MOVE_CANCELLED) {
    drainSerial();
    Serial.print("CANCELLED after ");
  } else if (result == MOVE_TIMEOUT) {
    Serial.print("ERROR: move timed out after ");
  } else {
    Serial.print("Done: moved ");
  }
  Serial.print(completed * MM_PER_STEP, 3);
  Serial.print(" mm in ");
  Serial.print(elapsed_s, 2);
  Serial.print(" s (avg ");
  Serial.print(elapsed_s > 0.0f ? completed * MM_PER_STEP / elapsed_s : 0.0f, 4);
  Serial.print(" mm/s). Position: ");
  printPosition(position_steps);
  Serial.println();
}

void printRateTooHigh(float rate) {
  Serial.print("ERROR: ");
  Serial.print(rate, 4);
  Serial.print(" mm/s exceeds the motor limit of ");
  Serial.print(MAX_RATE_MM_S, 2);
  Serial.print(" mm/s (");
  Serial.print(MAX_MOTOR_RPM, 0);
  Serial.println(" RPM). Rate unchanged.");
}

void printSettings() {
  Serial.print("Rate: ");
  Serial.print(rate_mm_s, 4);
  Serial.print(" mm/s (");
  Serial.print(rate_mm_s / MM_PER_STEP, 2);
  Serial.print(" steps/s, ");
  Serial.print(rate_mm_s / SCREW_LEAD_MM * 60.0f, 2);
  Serial.print(" RPM)   Distance: ");
  Serial.print(distance_mm, 3);
  Serial.print(" mm   Position: ");
  printPosition(position_steps);
  Serial.println();
}

void printMenu() {
  Serial.println();
  Serial.println("=== Dual-driver displacement trials (Mega 2560) ===");
  Serial.print("Sketch: ");
  Serial.println(SKETCH_NAME);
  Serial.print("  v<mm/s>  set displacement rate (e.g. v0.5, max ");
  Serial.print(MAX_RATE_MM_S, 1);
  Serial.println(")");
  Serial.println("  d<mm>    set trial distance (e.g. d20)");
  Serial.println("  o / c    run trial: open / close by distance at rate");
  Serial.println("  h        return to zero position");
  Serial.println("  z        set current position as zero");
  Serial.println("  ?        show this menu");
  Serial.println("  Any key during a trial cancels it.");
  printSettings();
}

void setup() {
  DDRA  |= STEP_MASK | DIR_MASK;     // D22-D25 as outputs
  PORTA &= ~(STEP_MASK | DIR_MASK);  // all LOW

  Serial.begin(115200);

  if (!initTimer(rate_mm_s / MM_PER_STEP)) {
    fatalError("ERROR: could not configure Timer1");
  }
  printMenu();
}

void loop() {
  if (!Serial.available()) return;

  char c = Serial.read();
  switch (c) {
    case 'v': case 'V': {
      float rate = Serial.parseFloat();
      if (rate > MAX_RATE_MM_S) {
        printRateTooHigh(rate);
      } else if (rate < MIN_RATE_MM_S) {
        Serial.print("ERROR: rate must be at least ");
        Serial.print(MIN_RATE_MM_S, 2);
        Serial.println(" mm/s. Rate unchanged.");
      } else {
        rate_mm_s = rate;
        setStepRate(rate_mm_s / MM_PER_STEP);
        Serial.print("Rate set to ");
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

    case 'h': case 'H':
      runTrial(labs(position_steps), position_steps < 0);
      break;

    case 'z': case 'Z':
      position_steps = 0;
      Serial.println("Position set to zero.");
      break;

    case '?': printMenu(); break;

    default: break;            // ignore newlines, spaces, etc.
  }
}
