/*
  Ultra-precision synchronized dual stepper control
  Arduino Mega 2560 (ATmega2560, 16 MHz) -- uses the 16-bit Timer1 compare
  interrupt for jitter-free pulse generation.

  Drivers: 2x Stepperonline DM542T V4.0 (one per motor)
  Motors:  2x Stepperonline 23HS30-3004S (1.8 deg, 3.0 A)

  Driver settings (must be IDENTICAL on both drivers):
    S2 selector: 5V   (factory default is 24V -- must be changed for 5V signals)
    SW1 OFF, SW2 OFF, SW3 ON  -> 2.37 A peak / 1.69 A RMS
    SW4 ON                    -> standstill current 90%
    SW5 OFF, SW6 ON, SW7 ON, SW8 ON -> 400 pulses/rev (matches STEPS_PER_REV)

  Wiring (each line goes through its own NPN transistor buffer):
    D22 (PA0) STEP_A -> NPN -> Driver A PUL-
    D23 (PA1) STEP_B -> NPN -> Driver B PUL-
    D24 (PA2) DIR_A  -> NPN -> Driver A DIR-
    D25 (PA3) DIR_B  -> NPN -> Driver B DIR-
    Both drivers' PUL+ and DIR+ -> +5V
    ENA+/ENA- left unconnected (drivers enabled)

  All four signals are on the same AVR port (PORTA), so the ISR toggles
  both STEP lines with a single register write. Both drivers receive their
  pulse edge at the same instant -- there is no relative timing skew between
  the two motors. Separate pins per driver also let single_driver_test.ino
  pulse one driver at a time without rewiring.
*/

// Port A bit masks (Mega pins 22-25)
const uint8_t STEP_A = _BV(PA0);   // D22
const uint8_t STEP_B = _BV(PA1);   // D23
const uint8_t DIR_A  = _BV(PA2);   // D24
const uint8_t DIR_B  = _BV(PA3);   // D25

const uint8_t STEP_MASK = STEP_A | STEP_B;
const uint8_t DIR_MASK  = DIR_A | DIR_B;

// Must match the microstep DIP switch setting on BOTH drivers -- keep identical.
const long STEPS_PER_REV = 400;

// Step rate used by the demo loop, in steps/sec (500 at 400 pulses/rev = 75 RPM).
const float STEP_RATE_HZ = 500.0f;

// Upper limit for the ISR-driven step rate. The ISR toggles the pins twice per
// step, so much faster than this and the CPU spends all its time in the ISR.
const float MAX_STEP_RATE_HZ = 20000.0f;

float current_rate_hz = 0.0f;
uint8_t timer_cs_bits = 0;   // Timer1 clock-select bits for the current rate

volatile bool step_state = false;
volatile long steps_remaining = 0;
volatile bool motion_active = false;

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

bool moveSteps(long steps, bool clockwise, float step_rate_hz) {
  if (steps <= 0 || step_rate_hz <= 0.0f) return true;

  if (step_rate_hz != current_rate_hz) {
    if (!setStepRate(step_rate_hz)) {
      Serial.println("ERROR: could not set step rate");
      return false;
    }
  }

  // Timer is stopped here, so it is safe to touch PORTA and the multi-byte counter.
  if (clockwise) PORTA |= DIR_MASK;
  else           PORTA &= ~DIR_MASK;
  delayMicroseconds(10);   // direction setup time before first pulse (DM542T V4.0 min 5 us)

  steps_remaining = steps;
  step_state = false;
  motion_active = true;

  TCNT1 = 0;
  TIFR1 = _BV(OCF1A);                  // clear any stale compare flag
  TIMSK1 |= _BV(OCIE1A);
  TCCR1B = _BV(WGM12) | timer_cs_bits; // start the clock

  // ISR does all the work; wait for the move to finish, with a timeout safeguard
  unsigned long timeout_ms = (unsigned long)(steps * 1000.0f / step_rate_hz) + 1000;
  unsigned long start_ms = millis();
  while (motion_active) {
    if (millis() - start_ms > timeout_ms) {
      stopTimer();
      motion_active = false;
      PORTA &= ~STEP_MASK;
      Serial.println("ERROR: move timed out");
      return false;
    }
  }

  stopTimer();
  return true;
}

void setup() {
  DDRA  |= STEP_MASK | DIR_MASK;     // D22-D25 as outputs
  PORTA &= ~(STEP_MASK | DIR_MASK);  // all LOW

  Serial.begin(115200);

  if (!initTimer(STEP_RATE_HZ)) {
    fatalError("ERROR: could not configure Timer1");
  }
}

void loop() {
  moveSteps(STEPS_PER_REV, true, STEP_RATE_HZ);    // one revolution forward
  delay(500);

  moveSteps(STEPS_PER_REV, false, STEP_RATE_HZ);   // one revolution back
  delay(500);
}
