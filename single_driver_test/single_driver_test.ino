/*
  Single-driver bring-up test
  Arduino Mega 2560 -- pulses ONE DM542T V4.0 driver at a time, chosen from
  the Serial Monitor, so each driver/motor can be checked on its own.

  Uses the same wiring, driver settings and Timer1 step engine as
  dual_stepper_mega.ino (direct, common-cathode -- no transistors needed):
    D22 (PA0) STEP_A -> Driver A PUL+      Driver A PUL- -> Mega GND
    D23 (PA1) STEP_B -> Driver B PUL+      Driver B PUL- -> Mega GND
    D24 (PA2) DIR_A  -> Driver A DIR+      Driver A DIR- -> Mega GND
    D25 (PA3) DIR_B  -> Driver B DIR+      Driver B DIR- -> Mega GND
    ENA+/ENA- left unconnected (drivers enabled)
  With only one driver connected, wire it as driver A: D22, D24 and GND.
  The driver that is not selected gets no pulses; its STEP and DIR stay LOW.

  Serial Monitor: 115200 baud. Commands:
    a / b      select driver A or B
    f / r      one revolution forward / reverse
    t          back-and-forth test (1 rev fwd, 1 rev back) x3
    s<number>  set step rate in steps/s, e.g. s1000
    ?          show menu and current settings
*/

// Port A bit masks (Mega pins 22-25)
const uint8_t STEP_A = _BV(PA0);   // D22
const uint8_t STEP_B = _BV(PA1);   // D23
const uint8_t DIR_A  = _BV(PA2);   // D24
const uint8_t DIR_B  = _BV(PA3);   // D25

const uint8_t ALL_MASK = STEP_A | STEP_B | DIR_A | DIR_B;

// Must match the microstep DIP switch setting on the driver under test.
const long STEPS_PER_REV = 400;

// Default step rate, in steps/sec (500 at 400 pulses/rev = 75 RPM).
const float DEFAULT_STEP_RATE_HZ = 500.0f;

// Upper limit for the ISR-driven step rate.
const float MAX_STEP_RATE_HZ = 20000.0f;

const int TEST_CYCLES = 3;

char selected_driver = 'A';
uint8_t step_mask = STEP_A;        // STEP bit of the selected driver only
uint8_t dir_mask  = DIR_A;
float step_rate_hz = DEFAULT_STEP_RATE_HZ;

float current_rate_hz = 0.0f;
uint8_t timer_cs_bits = 0;   // Timer1 clock-select bits for the current rate

volatile bool step_state = false;
volatile long steps_remaining = 0;
volatile bool motion_active = false;
volatile uint8_t isr_step_mask = STEP_A;

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
// Picks the smallest prescaler that fits in 16 bits.
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
      return true;
    }
  }
  return false;
}

bool moveSteps(long steps, bool clockwise, float rate_hz) {
  if (steps <= 0 || rate_hz <= 0.0f) return true;

  if (rate_hz != current_rate_hz) {
    if (!setStepRate(rate_hz)) {
      Serial.println("ERROR: could not set step rate");
      return false;
    }
  }

  // Timer is stopped here, so it is safe to touch PORTA and the multi-byte counter.
  PORTA &= ~ALL_MASK;          // other driver fully idle
  if (clockwise) PORTA |= dir_mask;
  delayMicroseconds(10);   // direction setup time before first pulse (DM542T V4.0 min 5 us)

  isr_step_mask = step_mask;
  steps_remaining = steps;
  step_state = false;
  motion_active = true;

  TCNT1 = 0;
  TIFR1 = _BV(OCF1A);                  // clear any stale compare flag
  TIMSK1 |= _BV(OCIE1A);
  TCCR1B = _BV(WGM12) | timer_cs_bits; // start the clock

  unsigned long timeout_ms = (unsigned long)(steps * 1000.0f / rate_hz) + 1000;
  unsigned long start_ms = millis();
  while (motion_active) {
    if (millis() - start_ms > timeout_ms) {
      stopTimer();
      motion_active = false;
      PORTA &= ~step_mask;
      Serial.println("ERROR: move timed out");
      return false;
    }
  }

  stopTimer();
  return true;
}

// Move one revolution and report how long it took.
void revolution(bool clockwise) {
  Serial.print("Driver ");
  Serial.print(selected_driver);
  Serial.print(clockwise ? ": forward " : ": reverse ");
  Serial.print(STEPS_PER_REV);
  Serial.print(" steps @ ");
  Serial.print(step_rate_hz, 1);
  Serial.print(" steps/s ... ");

  unsigned long start_us = micros();
  bool ok = moveSteps(STEPS_PER_REV, clockwise, step_rate_hz);
  unsigned long elapsed_us = micros() - start_us;

  if (ok) {
    Serial.print("done in ");
    Serial.print(elapsed_us / 1000.0f, 1);
    Serial.println(" ms");
  }
}

void selectDriver(char driver) {
  selected_driver = driver;
  step_mask = (driver == 'A') ? STEP_A : STEP_B;
  dir_mask  = (driver == 'A') ? DIR_A  : DIR_B;
  Serial.print("Selected driver ");
  Serial.println(selected_driver);
}

void printMenu() {
  Serial.println();
  Serial.println("=== Single-driver test (Mega 2560) ===");
  Serial.println("  a / b      select driver A (D22/D24) or B (D23/D25)");
  Serial.println("  f / r      one revolution forward / reverse");
  Serial.println("  t          back-and-forth test x3");
  Serial.println("  s<number>  set step rate in steps/s (e.g. s1000)");
  Serial.println("  ?          show this menu");
  Serial.print("Driver: ");
  Serial.print(selected_driver);
  Serial.print("   Rate: ");
  Serial.print(step_rate_hz, 1);
  Serial.print(" steps/s (");
  Serial.print(step_rate_hz * 60.0f / STEPS_PER_REV, 1);
  Serial.print(" RPM)   Steps/rev: ");
  Serial.println(STEPS_PER_REV);
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
        revolution(true);
        delay(500);
        revolution(false);
        delay(500);
      }
      break;

    case 's': case 'S': {
      float rate = Serial.parseFloat();
      if (rate > 0.0f && setStepRate(rate)) {
        step_rate_hz = rate;
        Serial.print("Step rate set to ");
        Serial.print(step_rate_hz, 1);
        Serial.print(" steps/s (");
        Serial.print(step_rate_hz * 60.0f / STEPS_PER_REV, 1);
        Serial.println(" RPM)");
      } else {
        Serial.print("Invalid rate; must be between 0.12 and ");
        Serial.print(MAX_STEP_RATE_HZ, 0);
        Serial.println(" steps/s");
        setStepRate(step_rate_hz);
      }
      break;
    }

    case '?': printMenu(); break;

    default: break;            // ignore newlines, spaces, etc.
  }
}
