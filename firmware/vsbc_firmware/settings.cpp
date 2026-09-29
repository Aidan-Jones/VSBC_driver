#include <EEPROM.h>
#include "config.h"
#include "settings.h"

Settings settings;
Params params = {0.0f, 0.0f, 0.0f, 0};

namespace {

const uint16_t EEPROM_MAGIC  = 0x5642;   // "VB"
const uint8_t  EEPROM_LAYOUT = 1;        // bump when Settings changes

struct Stored {
  uint16_t magic;
  uint8_t  layout;
  Settings s;
  uint8_t  crc;
};

uint8_t crc8(const uint8_t *data, size_t len) {
  uint8_t crc = 0;
  while (len--) {
    crc ^= *data++;
    for (uint8_t i = 0; i < 8; i++) crc = (crc & 0x80) ? (crc << 1) ^ 0x07 : crc << 1;
  }
  return crc;
}

enum KeyType : uint8_t { K_FLOAT, K_LONG, K_ULONG, K_BOOL };

struct KeyDef {
  const char *name;
  KeyType type;
  void *ptr;
  float lo, hi;
  uint8_t digits;
};

const KeyDef KEYS[] = {
  {"steps_per_mm", K_FLOAT, &settings.steps_per_mm, NOMINAL_STEPS_PER_MM / 2, NOMINAL_STEPS_PER_MM * 2, 4},
  {"load_scale",   K_FLOAT, &settings.load_scale,   -1.0e9f, 1.0e9f, 4},
  {"load_offset",  K_LONG,  &settings.load_offset,  -8388608.0f, 8388607.0f, 0},
  {"max_load",     K_FLOAT, &settings.max_load,     0.0f, 1.0e6f, 3},
  {"min_pos",      K_FLOAT, &settings.min_pos,      -10000.0f, 10000.0f, 4},
  {"max_pos",      K_FLOAT, &settings.max_pos,      -10000.0f, 10000.0f, 4},
  {"dir_invert",   K_BOOL,  &settings.dir_invert,   0.0f, 1.0f, 0},
  {"invert_b",     K_BOOL,  &settings.invert_b,     0.0f, 1.0f, 0},
  {"stop_load",    K_FLOAT, &params.stop_load,      0.0f, 1.0e6f, 3},
  {"break_drop",   K_FLOAT, &params.break_drop,     0.0f, 99.0f, 1},
  {"break_min",    K_FLOAT, &params.break_min,      0.0f, 1.0e6f, 3},
  {"watchdog_ms",  K_ULONG, &params.watchdog_ms,    0.0f, 600000.0f, 0},
};
const uint8_t KEY_COUNT = sizeof(KEYS) / sizeof(KEYS[0]);

const KeyDef *findKey(const char *name) {
  for (uint8_t i = 0; i < KEY_COUNT; i++) {
    if (strcasecmp(name, KEYS[i].name) == 0) return &KEYS[i];
  }
  return nullptr;
}

void printValue(const KeyDef &k, Print &out) {
  out.print(' ');
  out.print(k.name);
  out.print('=');
  switch (k.type) {
    case K_FLOAT: out.print(*(float *)k.ptr, k.digits); break;
    case K_LONG:  out.print(*(long *)k.ptr); break;
    case K_ULONG: out.print(*(unsigned long *)k.ptr); break;
    case K_BOOL:  out.print(*(uint8_t *)k.ptr); break;
  }
}

}  // namespace

void settingsDefaults() {
  settings.steps_per_mm = NOMINAL_STEPS_PER_MM;
  settings.load_scale   = 0.0f;
  settings.load_offset  = 0;
  settings.max_load     = 0.0f;
  settings.min_pos      = DEFAULT_MIN_POS_MM;
  settings.max_pos      = DEFAULT_MAX_POS_MM;
  settings.dir_invert   = 0;
  settings.invert_b     = 0;
}

bool settingsLoad() {
  Stored st;
  EEPROM.get(0, st);
  bool valid = st.magic == EEPROM_MAGIC && st.layout == EEPROM_LAYOUT &&
               st.crc == crc8((const uint8_t *)&st.s, sizeof(st.s)) &&
               st.s.steps_per_mm >= NOMINAL_STEPS_PER_MM / 2 &&
               st.s.steps_per_mm <= NOMINAL_STEPS_PER_MM * 2 &&
               st.s.min_pos < st.s.max_pos && !isnan(st.s.load_scale);
  if (!valid) {
    settingsDefaults();
    return false;
  }
  settings = st.s;
  return true;
}

void settingsSave() {
  Stored st;
  st.magic = EEPROM_MAGIC;
  st.layout = EEPROM_LAYOUT;
  st.s = settings;
  st.crc = crc8((const uint8_t *)&st.s, sizeof(st.s));
  EEPROM.put(0, st);   // only writes bytes that changed
}

bool loadCalibrated() {
  return settings.load_scale != 0.0f && !isnan(settings.load_scale) && !isinf(settings.load_scale);
}

SetResult settingsSet(const char *key, const char *value) {
  const KeyDef *k = findKey(key);
  if (!k) return SET_UNKNOWN;

  char *end;
  double v = strtod(value, &end);
  if (end == value || *end != '\0' || isnan(v) || isinf(v)) return SET_BAD_VALUE;
  if (v < k->lo || v > k->hi) return SET_RANGE;
  if (k->type == K_BOOL && v != 0.0 && v != 1.0) return SET_RANGE;
  if (k->type == K_ULONG && v != 0.0 && v < 100.0) return SET_RANGE;   // watchdog: 0 or >= 100 ms

  // The travel window must stay non-empty.
  if (k->ptr == &settings.min_pos && v >= settings.max_pos) return SET_RANGE;
  if (k->ptr == &settings.max_pos && v <= settings.min_pos) return SET_RANGE;

  switch (k->type) {
    case K_FLOAT: *(float *)k->ptr = (float)v; break;
    case K_LONG:  *(long *)k->ptr = lround(v); break;
    case K_ULONG: *(unsigned long *)k->ptr = (unsigned long)lround(v); break;
    case K_BOOL:  *(uint8_t *)k->ptr = v != 0.0 ? 1 : 0; break;
  }
  return SET_OK;
}

bool settingsPrintKey(const char *key, Print &out) {
  const KeyDef *k = findKey(key);
  if (!k) return false;
  printValue(*k, out);
  return true;
}

void settingsPrintAll(Print &out) {
  for (uint8_t i = 0; i < KEY_COUNT; i++) printValue(KEYS[i], out);
}
