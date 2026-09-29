/*
  Compile-time configuration for the VSBC tensile tester firmware.

  Wiring and mechanics live here. Values that are calibrated on the machine
  (steps per mm, load-cell scale, travel limits, directions) are stored in
  EEPROM instead and changed at run time with SET / SAVE -- see settings.h.
*/
#pragma once
#include <Arduino.h>

#define FIRMWARE_NAME    "vsbc_firmware"
#define FIRMWARE_VERSION "2.0.0"
#define PROTOCOL_VERSION 1

const unsigned long SERIAL_BAUD = 115200;

// ------------------------------------------------------------------ motors --
// Two Stepperonline DM542T V4.0 drivers, wired directly (common cathode):
// Mega pin -> PUL+ / DIR+, PUL- / DIR- -> Mega GND, ENA not connected.
// All four signals are on PORTA, so the Timer1 ISR steps both motors with a
// single register write -- both drivers see their edge on the same clock cycle.
const uint8_t STEP_A = _BV(PA0);   // D22 -> driver A PUL+
const uint8_t STEP_B = _BV(PA1);   // D23 -> driver B PUL+
const uint8_t DIR_A  = _BV(PA2);   // D24 -> driver A DIR+
const uint8_t DIR_B  = _BV(PA3);   // D25 -> driver B DIR+

// Must match the microstep DIP switches (SW5-SW8) on BOTH drivers.
const long  STEPS_PER_REV = 400;
// Ball-screw lead: linear travel per motor revolution (direct coupling).
const float SCREW_LEAD_MM = 5.0f;
// Nominal steps per mm (80). The calibrated value is the setting steps_per_mm.
const float NOMINAL_STEPS_PER_MM = STEPS_PER_REV / SCREW_LEAD_MM;

// Motor speed limit. Moves start at full rate with no ramp (a ramp would break
// the constant-rate guarantee), so the motor must start and hold the rate from
// standstill under load. 23HS30-3004S at the 2.37 A driver setting: torque
// starts to fall above ~320 RPM on 24 V (~630 RPM on 48 V); 120 RPM keeps close
// to full low-speed torque on any supply in the DM542T range. docs/hardware.md
// has the full derivation.
const float MAX_MOTOR_RPM    = 120.0f;
const float MAX_STEP_RATE_HZ = MAX_MOTOR_RPM / 60.0f * STEPS_PER_REV;   // 800 steps/s

// Slowest crosshead rate accepted. Timer1 bottoms out near 0.12 steps/s.
const float MIN_RATE_MM_S     = 0.002f;
const float DEFAULT_RATE_MM_S = 0.5f;

// Ceiling for the ISR itself (it runs twice per step).
const float TIMER_MAX_STEP_RATE_HZ = 20000.0f;

// --------------------------------------------------------------- load cell --
#define LOADCELL_HX711 1
#define LOADCELL_TYPE  LOADCELL_HX711   // selects loadcell_hx711.cpp

const uint8_t HX711_DOUT_PIN = 26;   // HX711 DT  (PA4)
const uint8_t HX711_SCK_PIN  = 27;   // HX711 SCK (PA5)
// Clock pulses after the 24 data bits: 1 = channel A gain 128,
// 2 = channel B gain 32, 3 = channel A gain 64.
const uint8_t HX711_GAIN_PULSES = 1;

// No sample for this long = load cell lost.
const unsigned long LOADCELL_TIMEOUT_MS = 500;
// Samples averaged by TARE and CAL.
const uint8_t AVERAGE_SAMPLES = 16;
// CAL needs at least this many counts of change from the tare value.
const long MIN_CAL_COUNTS = 1000;

// ---------------------------------------------------------- safety inputs --
// Optional switches, closing to GND when triggered (INPUT_PULLUP).
// -1 = not fitted. Polled every loop().
const int8_t LIMIT_MIN_PIN = -1;   // stops motion in the - direction
const int8_t LIMIT_MAX_PIN = -1;   // stops motion in the + direction
const int8_t ESTOP_PIN     = -1;   // stops all motion while active

// --------------------------------------------------------------- telemetry --
const unsigned long PROGRESS_INTERVAL_MS  = 500;    // STREAM OFF: D line period while moving
const unsigned long NO_LOADCELL_STREAM_MS = 100;    // STREAM ON without a load cell
const unsigned long OVERLOAD_WARN_MS      = 1000;   // idle overload warning period

// ---------------------------------------------------------- EEPROM defaults --
const float DEFAULT_MIN_POS_MM = -100.0f;
const float DEFAULT_MAX_POS_MM = 100.0f;
