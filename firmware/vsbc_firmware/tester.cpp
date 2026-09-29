#include "config.h"
#include "tester.h"
#include "loadcell.h"
#include "motion.h"
#include "settings.h"

namespace {

bool stream_on = false;

long    last_raw  = 0;
float   last_load = NAN;   // unfiltered, for the D lines
float   filt_load = NAN;   // median of the last three samples, for safety decisions
long    hist[3]   = {0, 0, 0};
uint8_t hist_n    = 0;
bool    lc_alive  = false; // load-cell state last reported to the host

unsigned long last_host_ms          = 0;
unsigned long last_emit_ms          = 0;
unsigned long last_overload_warn_ms = 0;
unsigned long dropped               = 0;

// Current move
float   start_mag    = 0.0f;   // |load| when the move started
bool    sat_at_start = false;  // ADC already saturated when the move started
float   peak         = NAN;    // RUN: peak load in the run direction
int8_t  run_dir      = 0;
uint8_t break_count  = 0;

// TARE / CAL averaging
tester::AvgKind avg_kind  = tester::AVG_NONE;
float           avg_known = 0.0f;
long            avg_buf[AVERAGE_SAMPLES];
uint8_t         avg_n     = 0;

// Optional switch inputs
bool estop   = false;
bool lim_min = false;
bool lim_max = false;

float toNewtons(long counts) {
  if (!loadCalibrated()) return NAN;
  return (counts - settings.load_offset) / settings.load_scale;
}

long median3(long a, long b, long c) {
  long t;
  if (a > b) { t = a; a = b; b = t; }
  if (b > c) { t = b; b = c; c = t; }
  if (a > b) { t = a; a = b; b = t; }
  return b;
}

void appendNumber(char *line, float v, uint8_t digits, float limit) {
  char num[20];
  if (isnan(v)) {
    strcat(line, "nan");
  } else if (v >= limit) {
    strcat(line, "inf");
  } else if (v <= -limit) {
    strcat(line, "-inf");
  } else {
    dtostrf(v, 1, digits, num);
    strcat(line, num);
  }
}

// D <t_ms> <pos_mm> <load_N> <raw> <state>
void emitData() {
  last_emit_ms = millis();
  char line[72];
  char num[12];
  strcpy(line, "D ");
  ultoa(last_emit_ms, num, 10);
  strcat(line, num);
  strcat(line, " ");
  appendNumber(line, motion::position(), 4, 1.0e6f);
  strcat(line, " ");
  appendNumber(line, lc_alive ? last_load : NAN, 3, 1.0e7f);
  strcat(line, " ");
  ltoa(lc_alive ? last_raw : 0L, num, 10);
  strcat(line, num);
  size_t n = strlen(line);
  line[n] = ' ';
  line[n + 1] = tester::stateChar();
  line[n + 2] = '\0';

  // Never block the loop on telemetry: skip the line if it does not fit.
  if ((size_t)Serial.availableForWrite() < n + 4) {
    dropped++;
    return;
  }
  Serial.println(line);
}

void collectAverage(long raw) {
  avg_buf[avg_n++] = raw;
  if (avg_n < AVERAGE_SAMPLES) return;

  // Sum differences from the first sample so the float maths stays exact.
  long base = avg_buf[0];
  long sum = 0;
  for (uint8_t i = 0; i < avg_n; i++) sum += avg_buf[i] - base;
  float mean_rel = (float)sum / avg_n;
  float var = 0.0f;
  for (uint8_t i = 0; i < avg_n; i++) {
    float d = (avg_buf[i] - base) - mean_rel;
    var += d * d;
  }
  float noise = sqrt(var / (avg_n - 1));
  float mean = base + mean_rel;

  tester::AvgKind kind = avg_kind;
  avg_kind = tester::AVG_NONE;

  if (kind == tester::AVG_TARE) {
    settings.load_offset = lround(mean);
    Serial.print(F("OK TARE offset="));
    Serial.print(settings.load_offset);
    Serial.print(F(" noise="));
    Serial.println(noise, 1);
    return;
  }

  float delta = mean - settings.load_offset;
  if (fabs(delta) < MIN_CAL_COUNTS) {
    Serial.println(F("ERR CAL RANGE load change too small: TARE with no load first, then apply a larger known load"));
    return;
  }
  settings.load_scale = delta / avg_known;
  Serial.print(F("OK CAL scale="));
  Serial.print(settings.load_scale, 4);
  Serial.print(F(" known="));
  Serial.println(avg_known, 3);
  Serial.println(F("# Calibration changed in RAM. SAVE to keep it."));
}

void checkSafety(long raw) {
  if (loadcell::saturated(raw) && !sat_at_start) {
    motion::stop(motion::END_OVERLOAD);
    return;
  }
  if (isnan(filt_load)) return;   // not calibrated: nothing to compare against

  // RUN: track the peak first, so a stop below includes the sample that caused it.
  bool running = motion::mode() == motion::MODE_RUN;
  float v = run_dir * filt_load;   // load in the run direction (tension for RUN +)
  if (running && (isnan(peak) || v > peak)) peak = v;

  float mag = fabs(filt_load);
  // Overload: only while |load| is rising past where the move started, so a
  // move that backs out of an overload is still allowed.
  if (settings.max_load > 0.0f && mag >= settings.max_load &&
      mag > start_mag + 0.01f * settings.max_load) {
    motion::stop(motion::END_OVERLOAD);
    return;
  }
  if (!running) return;

  if (params.stop_load > 0.0f && mag >= params.stop_load) {
    motion::stop(motion::END_LOAD);
    return;
  }

  if (params.break_drop > 0.0f && params.break_min > 0.0f && peak >= params.break_min &&
      v < peak * (1.0f - params.break_drop / 100.0f)) {
    if (++break_count >= 2) motion::stop(motion::END_BREAK);
  } else {
    break_count = 0;
  }
}

void checkIdleOverload() {
  if (settings.max_load <= 0.0f || isnan(filt_load) || fabs(filt_load) < settings.max_load) return;
  unsigned long now = millis();
  if (now - last_overload_warn_ms < OVERLOAD_WARN_MS) return;
  last_overload_warn_ms = now;
  Serial.print(F("EVT OVERLOAD load="));
  Serial.println(filt_load, 3);
}

void onSample(long raw) {
  last_raw = raw;
  last_load = toNewtons(raw);
  hist[0] = hist[1];
  hist[1] = hist[2];
  hist[2] = raw;
  if (hist_n < 3) hist_n++;
  filt_load = toNewtons(hist_n < 3 ? raw : median3(hist[0], hist[1], hist[2]));

  if (!lc_alive) {
    lc_alive = true;
    Serial.println(F("EVT LOADCELL state=OK"));
  }
  if (avg_kind != tester::AVG_NONE) collectAverage(raw);
  if (motion::busy()) checkSafety(raw);
  else checkIdleOverload();
  if (stream_on) emitData();
}

void checkLoadCell() {
  if (!lc_alive || loadcell::alive()) return;
  lc_alive = false;
  hist_n = 0;
  last_load = NAN;
  filt_load = NAN;
  Serial.println(F("EVT LOADCELL state=LOST"));
  if (motion::mode() == motion::MODE_RUN) motion::stop(motion::END_LOADCELL);
  if (avg_kind != tester::AVG_NONE) {
    Serial.print(F("ERR "));
    Serial.print(avg_kind == tester::AVG_TARE ? F("TARE") : F("CAL"));
    Serial.println(F(" NOLOAD load cell stopped responding"));
    avg_kind = tester::AVG_NONE;
  }
}

bool inputActive(int8_t pin) {
  return pin >= 0 && digitalRead((uint8_t)pin) == LOW;
}

void reportInput(const __FlashStringHelper *name, bool state) {
  Serial.print(F("EVT INPUT name="));
  Serial.print(name);
  Serial.print(F(" state="));
  Serial.println(state ? 1 : 0);
}

void checkInputs() {
  if (ESTOP_PIN >= 0) {
    bool a = inputActive(ESTOP_PIN);
    if (a != estop) { estop = a; reportInput(F("ESTOP"), a); }
    if (estop) motion::stop(motion::END_ESTOP);
  }
  if (LIMIT_MIN_PIN >= 0) {
    bool a = inputActive(LIMIT_MIN_PIN);
    if (a != lim_min) { lim_min = a; reportInput(F("LIMIT_MIN"), a); }
    if (lim_min && motion::direction() < 0) motion::stop(motion::END_LIMIT);
  }
  if (LIMIT_MAX_PIN >= 0) {
    bool a = inputActive(LIMIT_MAX_PIN);
    if (a != lim_max) { lim_max = a; reportInput(F("LIMIT_MAX"), a); }
    if (lim_max && motion::direction() > 0) motion::stop(motion::END_LIMIT);
  }
}

void checkWatchdog() {
  if (params.watchdog_ms == 0 || !motion::busy()) return;
  if (millis() - last_host_ms > params.watchdog_ms) motion::stop(motion::END_WATCHDOG);
}

void reportMoveEnd(const motion::MoveEnd &end) {
  Serial.print(F("EVT MOVE_END reason="));
  Serial.print(motion::reasonName(end.reason));
  Serial.print(F(" mode="));
  Serial.print(end.mode == motion::MODE_RUN ? F("RUN") : F("MOVE"));
  Serial.print(F(" pos="));
  Serial.print(motion::position(), 4);
  Serial.print(F(" moved="));
  Serial.print(end.moved_mm, 4);
  Serial.print(F(" time="));
  Serial.print(end.time_s, 2);
  Serial.print(F(" peak="));
  Serial.println(isnan(peak) ? NAN : peak * run_dir, 3);
}

void periodicTelemetry() {
  unsigned long now = millis();
  if (stream_on) {
    if (!lc_alive && now - last_emit_ms >= NO_LOADCELL_STREAM_MS) emitData();
  } else if (motion::busy() && now - last_emit_ms >= PROGRESS_INTERVAL_MS) {
    emitData();
  }
}

}  // namespace

namespace tester {

void begin() {
  if (ESTOP_PIN >= 0) pinMode((uint8_t)ESTOP_PIN, INPUT_PULLUP);
  if (LIMIT_MIN_PIN >= 0) pinMode((uint8_t)LIMIT_MIN_PIN, INPUT_PULLUP);
  if (LIMIT_MAX_PIN >= 0) pinMode((uint8_t)LIMIT_MAX_PIN, INPUT_PULLUP);
  last_host_ms = millis();
}

void poll() {
  long raw_value;
  if (loadcell::poll(raw_value)) onSample(raw_value);
  checkLoadCell();
  checkInputs();
  checkWatchdog();
  motion::MoveEnd end;
  if (motion::service(end)) reportMoveEnd(end);
  periodicTelemetry();
}

void hostActivity() {
  last_host_ms = millis();
}

void beginMove() {
  // Largest |load| in the filter window: the median filter lags one sample, so
  // a move that starts right after an overload stop must not look like a rise.
  start_mag = 0.0f;
  for (uint8_t i = 3 - hist_n; i < 3; i++) {
    float l = toNewtons(hist[i]);
    if (!isnan(l) && fabs(l) > start_mag) start_mag = fabs(l);
  }
  sat_at_start = lc_alive && loadcell::saturated(last_raw);
  peak = NAN;
  run_dir = motion::direction();
  break_count = 0;
  last_emit_ms = millis();
}

bool streaming() {
  return stream_on;
}

void setStreaming(bool on) {
  stream_on = on;
  last_emit_ms = millis();
}

float load() {
  return lc_alive ? last_load : NAN;
}

long raw() {
  return lc_alive ? last_raw : 0L;
}

bool loadCellAlive() {
  return lc_alive;
}

unsigned long droppedLines() {
  return dropped;
}

bool startAverage(AvgKind kind, float known_n) {
  if (!lc_alive) return false;
  avg_kind = kind;
  avg_known = known_n;
  avg_n = 0;
  return true;
}

bool averaging() {
  return avg_kind != AVG_NONE;
}

bool estopActive() {
  return estop;
}

bool limitBlocks(int8_t dir) {
  return (dir < 0 && lim_min) || (dir > 0 && lim_max);
}

char stateChar() {
  switch (motion::mode()) {
    case motion::MODE_MOVE: return 'M';
    case motion::MODE_RUN:  return 'R';
    default:                return 'I';
  }
}

}  // namespace tester
