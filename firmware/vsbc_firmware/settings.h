/*
  Machine settings. The Settings struct is saved in EEPROM by SAVE and is the
  source of truth for calibration; Params are per-test values the host sets
  before a RUN and are never saved. docs/protocol.md lists every key.
*/
#pragma once
#include <Arduino.h>

struct Settings {
  float   steps_per_mm;   // crosshead steps per mm (nominal 80)
  float   load_scale;     // load-cell counts per newton; 0 = not calibrated
  long    load_offset;    // load-cell counts at zero load (TARE)
  float   max_load;       // N; any move stops when |load| reaches this. 0 = off
  float   min_pos;        // mm; soft travel limits, relative to the reference zero
  float   max_pos;
  uint8_t dir_invert;     // 1 = DIR low moves + (tension) instead of DIR high
  uint8_t invert_b;       // 1 = motor B turns opposite to motor A
};

struct Params {
  float         stop_load;     // N; RUN stops when |load| reaches this. 0 = off
  float         break_drop;    // %; RUN stops when load falls this far below its peak. 0 = off
  float         break_min;     // N; peak needed before break detection arms. 0 = off
  unsigned long watchdog_ms;   // stop a move if the host is silent this long. 0 = off
};

extern Settings settings;
extern Params params;

void settingsDefaults();
bool settingsLoad();      // false = EEPROM blank or from an older layout; defaults applied
void settingsSave();
bool loadCalibrated();

enum SetResult : uint8_t { SET_OK, SET_UNKNOWN, SET_BAD_VALUE, SET_RANGE };

SetResult settingsSet(const char *key, const char *value);
// Prints " key=value" for one key (false if unknown) or for all of them.
bool settingsPrintKey(const char *key, Print &out);
void settingsPrintAll(Print &out);
