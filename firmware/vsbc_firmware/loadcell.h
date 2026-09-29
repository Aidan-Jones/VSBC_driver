/*
  Load-cell ADC interface. loadcell_hx711.cpp implements it for an HX711; a
  different ADC (NAU7802, ADS1232, ...) only needs another .cpp that implements
  these functions, selected with LOADCELL_TYPE in config.h.
*/
#pragma once
#include <Arduino.h>

namespace loadcell {

void begin();

// Non-blocking. Returns true and fills `raw` when a new conversion was read.
bool poll(long &raw);

// True if a sample arrived within LOADCELL_TIMEOUT_MS.
bool alive();

// Measured sample rate in Hz (0 until two samples have arrived).
float rateHz();

// True if `raw` is at the ADC's full-scale limit (load beyond range).
bool saturated(long raw);

const char *name();

}  // namespace loadcell
