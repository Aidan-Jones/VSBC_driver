/*
  Timer1 step engine. Both drivers get their STEP pulses from the Timer1
  compare interrupt, so the displacement rate is set by a hardware timer, not
  by loop() timing, and does not change with load (as long as the motors do
  not stall -- the system is open loop).
*/
#pragma once
#include <Arduino.h>

namespace motion {

enum Mode : uint8_t { MODE_IDLE, MODE_MOVE, MODE_RUN };

// Why a move ended. reasonName() gives the protocol word.
enum Reason : uint8_t {
  END_DONE, END_STOP, END_BREAK, END_LOAD, END_OVERLOAD,
  END_LOADCELL, END_WATCHDOG, END_TIMEOUT, END_LIMIT, END_ESTOP
};

enum StartResult : uint8_t { START_OK, START_BUSY, START_LIMIT, START_RATE };

const uint8_t MOTOR_A   = 1;
const uint8_t MOTOR_B   = 2;
const uint8_t MOTORS_AB = MOTOR_A | MOTOR_B;

struct MoveEnd {
  Reason reason;
  Mode mode;
  float moved_mm;   // signed
  float time_s;
};

bool begin();                   // pins + Timer1; false if the timer cannot be set up

bool setRate(float mm_s);       // only while idle; false if out of range
float rate();                   // requested crosshead rate, mm/s
float actualRate();             // rate the timer really produces, mm/s
float maxRate();                // mm/s at the motor speed limit

// Move to an absolute target (mm) at the set rate. Returns immediately; the
// ISR does the stepping. A zero-length move is allowed and ends at once.
StartResult start(float target_mm, Mode mode);
void stop(Reason why);          // safe to call at any time
// Call from loop(). Returns true (once) when a move has ended.
bool service(MoveEnd &end);

Mode mode();
bool busy();
int8_t direction();             // +1 / -1 during a move, 0 when idle
long moveSteps();               // length of the current / last move in steps
float expectedSeconds();        // expected duration of the current / last move

float position();               // live crosshead position, mm
void setPosition(float mm);     // ZERO: also marks the position referenced
bool referenced();

void selectMotors(uint8_t mask);   // MOTOR_A, MOTOR_B or MOTORS_AB; only while idle
uint8_t motors();

const char *reasonName(Reason r);

}  // namespace motion
