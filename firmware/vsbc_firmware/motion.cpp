#include <util/atomic.h>
#include "config.h"
#include "motion.h"
#include "settings.h"

namespace {

const uint8_t STEP_MASK_ALL = STEP_A | STEP_B;
const uint8_t DIR_MASK_ALL  = DIR_A | DIR_B;

float   rate_mm_s      = DEFAULT_RATE_MM_S;   // requested crosshead rate
float   actual_rate_hz = 0.0f;                // step rate the timer really produces
uint8_t timer_cs_bits  = 0;                   // Timer1 clock-select bits for that rate

volatile bool    step_state      = false;
volatile long    steps_remaining = 0;
volatile bool    motion_active   = false;
volatile uint8_t isr_step_mask   = STEP_MASK_ALL;

uint8_t motor_mask    = motion::MOTORS_AB;
long    pos_steps[2]  = {0, 0};   // motor A, motor B; + = tension
bool    is_referenced = false;

motion::Mode   cur_mode        = motion::MODE_IDLE;
long           move_steps      = 0;
int8_t         move_dir        = 0;
long           move_start[2]   = {0, 0};
unsigned long  move_start_ms   = 0;
unsigned long  move_timeout_ms = 0;
bool           half_pulse      = false;
bool           stop_pending    = false;
motion::Reason stop_reason     = motion::END_DONE;

void stopTimer() {
  TCCR1B = _BV(WGM12);          // CTC mode, clock stopped
  TIMSK1 &= ~_BV(OCIE1A);
}

// Timer1 in CTC mode interrupts every (OCR1A + 1) timer ticks. The ISR toggles
// the STEP pins twice per pulse (rising edge, then falling edge), so the
// compare rate is 2x the step rate. Picks the smallest prescaler that fits in
// 16 bits. Only call while the timer is stopped.
bool setStepRate(float step_rate_hz) {
  if (!(step_rate_hz > 0.0f) || step_rate_hz > TIMER_MAX_STEP_RATE_HZ) return false;

  const uint16_t prescalers[] = {1, 8, 64, 256, 1024};
  const uint8_t  cs_bits[]    = {_BV(CS10), _BV(CS11), _BV(CS11) | _BV(CS10),
                                 _BV(CS12), _BV(CS12) | _BV(CS10)};

  for (uint8_t i = 0; i < 5; i++) {
    uint32_t ticks = (uint32_t)(F_CPU / (2.0f * prescalers[i] * step_rate_hz) + 0.5f);
    if (ticks >= 1 && ticks <= 65536UL) {
      OCR1A = ticks - 1;
      timer_cs_bits = cs_bits[i];
      actual_rate_hz = F_CPU / (2.0f * prescalers[i] * ticks);
      return true;
    }
  }
  return false;                 // slower than ~0.12 steps/s
}

int reportedMotor() {
  return motor_mask == motion::MOTOR_B ? 1 : 0;
}

long stepsDone() {
  long remaining;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { remaining = steps_remaining; }
  return move_steps - remaining;
}

}  // namespace

ISR(TIMER1_COMPA_vect) {
  if (!motion_active) return;

  step_state = !step_state;
  PORTA ^= isr_step_mask;          // selected STEP lines change on the same clock cycle

  if (!step_state) {               // falling edge = one complete pulse
    steps_remaining--;
    if (steps_remaining <= 0) {
      motion_active = false;
      PORTA &= ~isr_step_mask;
      stopTimer();
    }
  }
}

namespace motion {

bool begin() {
  DDRA  |= STEP_MASK_ALL | DIR_MASK_ALL;      // D22-D25 as outputs
  PORTA &= ~(STEP_MASK_ALL | DIR_MASK_ALL);   // all LOW
  TCCR1A = 0;
  stopTimer();
  TCNT1 = 0;
  return setRate(rate_mm_s);
}

float maxRate() {
  return MAX_STEP_RATE_HZ / settings.steps_per_mm;
}

bool setRate(float mm_s) {
  if (cur_mode != MODE_IDLE) return false;
  if (!(mm_s >= MIN_RATE_MM_S) || mm_s > maxRate() * 1.0001f) return false;
  if (!setStepRate(mm_s * settings.steps_per_mm)) return false;
  rate_mm_s = mm_s;
  return true;
}

float rate() {
  return rate_mm_s;
}

float actualRate() {
  return actual_rate_hz / settings.steps_per_mm;
}

StartResult start(float target_mm, Mode mode) {
  if (cur_mode != MODE_IDLE) return START_BUSY;
  // Re-apply the rate in case steps_per_mm changed since RATE.
  if (!setStepRate(rate_mm_s * settings.steps_per_mm)) return START_RATE;

  int ref = reportedMotor();
  float cur_mm = pos_steps[ref] / settings.steps_per_mm;
  // Soft limits: refuse a target outside the window unless the move heads back toward it.
  if ((target_mm > settings.max_pos && target_mm > cur_mm) ||
      (target_mm < settings.min_pos && target_mm < cur_mm)) {
    return START_LIMIT;
  }

  long delta = lround(target_mm * settings.steps_per_mm) - pos_steps[ref];
  bool plus = delta >= 0;
  bool dir_a_high = (plus != (settings.dir_invert != 0));
  bool dir_b_high = (dir_a_high != (settings.invert_b != 0));
  uint8_t dir_bits = (dir_a_high ? DIR_A : 0) | (dir_b_high ? DIR_B : 0);

  // The timer is stopped, so PORTA and the multi-byte counters are safe to touch.
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { PORTA = (PORTA & ~DIR_MASK_ALL) | dir_bits; }
  delayMicroseconds(10);   // DIR setup time before the first pulse (DM542T V4.0 min 5 us)

  move_steps = labs(delta);
  move_dir = plus ? 1 : -1;
  move_start[0] = pos_steps[0];
  move_start[1] = pos_steps[1];
  move_start_ms = millis();
  move_timeout_ms = (unsigned long)(move_steps * 1000.0f / actual_rate_hz) + 1000;
  half_pulse = false;
  stop_pending = false;
  cur_mode = mode;
  if (motor_mask != MOTORS_AB && move_steps > 0) is_referenced = false;   // crosshead racked

  steps_remaining = move_steps;
  if (move_steps > 0) {
    isr_step_mask = ((motor_mask & MOTOR_A) ? STEP_A : 0) | ((motor_mask & MOTOR_B) ? STEP_B : 0);
    step_state = false;
    motion_active = true;
    TCNT1 = 0;
    TIFR1 = _BV(OCF1A);                    // clear any stale compare flag
    TIMSK1 |= _BV(OCIE1A);
    TCCR1B = _BV(WGM12) | timer_cs_bits;   // start the clock
  }
  return START_OK;
}

void stop(Reason why) {
  if (cur_mode == MODE_IDLE) return;
  bool was_active;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    was_active = motion_active;
    if (was_active) {
      stopTimer();
      motion_active = false;
      if (step_state) half_pulse = true;   // the driver already counted this rising edge
      step_state = false;
      PORTA &= ~STEP_MASK_ALL;
    }
  }
  if (was_active && !stop_pending) {
    stop_pending = true;
    stop_reason = why;
  }
}

bool service(MoveEnd &end) {
  if (cur_mode == MODE_IDLE) return false;
  if (motion_active) {
    if (millis() - move_start_ms <= move_timeout_ms) return false;
    stop(END_TIMEOUT);
  }

  long done = stepsDone() + (half_pulse ? 1 : 0);
  if (done > move_steps) done = move_steps;
  if (motor_mask & MOTOR_A) pos_steps[0] = move_start[0] + move_dir * done;
  if (motor_mask & MOTOR_B) pos_steps[1] = move_start[1] + move_dir * done;

  end.reason = stop_pending ? stop_reason : END_DONE;
  end.mode = cur_mode;
  end.moved_mm = move_dir * done / settings.steps_per_mm;
  end.time_s = (millis() - move_start_ms) / 1000.0f;

  cur_mode = MODE_IDLE;
  move_dir = 0;
  half_pulse = false;
  stop_pending = false;
  return true;
}

Mode mode() {
  return cur_mode;
}

bool busy() {
  return cur_mode != MODE_IDLE;
}

int8_t direction() {
  return move_dir;
}

long moveSteps() {
  return move_steps;
}

float expectedSeconds() {
  return actual_rate_hz > 0.0f ? move_steps / actual_rate_hz : 0.0f;
}

float position() {
  int ref = reportedMotor();
  long p = pos_steps[ref];
  if (cur_mode != MODE_IDLE) p = move_start[ref] + move_dir * stepsDone();
  return p / settings.steps_per_mm;
}

void setPosition(float mm) {
  long s = lround(mm * settings.steps_per_mm);
  pos_steps[0] = s;
  pos_steps[1] = s;
  is_referenced = true;
}

bool referenced() {
  return is_referenced;
}

void selectMotors(uint8_t mask) {
  if (cur_mode == MODE_IDLE && (mask & MOTORS_AB)) motor_mask = mask & MOTORS_AB;
}

uint8_t motors() {
  return motor_mask;
}

const char *reasonName(Reason r) {
  switch (r) {
    case END_DONE:     return "DONE";
    case END_STOP:     return "STOP";
    case END_BREAK:    return "BREAK";
    case END_LOAD:     return "LOAD";
    case END_OVERLOAD: return "OVERLOAD";
    case END_LOADCELL: return "LOADCELL";
    case END_WATCHDOG: return "WATCHDOG";
    case END_TIMEOUT:  return "TIMEOUT";
    case END_LIMIT:    return "LIMIT";
    case END_ESTOP:    return "ESTOP";
  }
  return "?";
}

}  // namespace motion
