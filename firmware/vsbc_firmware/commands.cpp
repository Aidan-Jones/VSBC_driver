#include "config.h"
#include "commands.h"
#include "loadcell.h"
#include "motion.h"
#include "settings.h"
#include "tester.h"

namespace {

char          line[64];
uint8_t       line_len      = 0;
bool          line_overflow = false;
unsigned long last_char_ms  = 0;

// A line typed without any line ending (Serial Monitor "No line ending")
// ends after this much silence.
const unsigned long LINE_IDLE_MS = 100;
const uint8_t MAX_TOKENS = 4;

struct Command;
typedef void (*Handler)(const Command &cmd, uint8_t argc, char **argv);

struct Command {
  const char *name;
  const char *alias;
  Handler handler;
  bool any_time;   // carried out while a move is running
};

// ------------------------------------------------------------------ replies --

void ok(const Command &c) {
  Serial.print(F("OK "));
  Serial.print(c.name);
}

// Starts an ERR line; the caller prints the message and the newline.
void errStart(const Command &c, const __FlashStringHelper *code) {
  Serial.print(F("ERR "));
  Serial.print(c.name);
  Serial.print(' ');
  Serial.print(code);
  Serial.print(' ');
}

void err(const Command &c, const __FlashStringHelper *code, const __FlashStringHelper *msg) {
  errStart(c, code);
  Serial.println(msg);
}

void kvf(const __FlashStringHelper *key, float value, uint8_t digits) {
  Serial.print(' ');
  Serial.print(key);
  Serial.print('=');
  Serial.print(value, digits);
}

void kvl(const __FlashStringHelper *key, long value) {
  Serial.print(' ');
  Serial.print(key);
  Serial.print('=');
  Serial.print(value);
}

void kvu(const __FlashStringHelper *key, unsigned long value) {
  Serial.print(' ');
  Serial.print(key);
  Serial.print('=');
  Serial.print(value);
}

void kvs(const __FlashStringHelper *key, const char *value) {
  Serial.print(' ');
  Serial.print(key);
  Serial.print('=');
  Serial.print(value);
}

bool parseNumber(const char *s, float &out) {
  char *end;
  double v = strtod(s, &end);
  if (end == s || *end != '\0' || isnan(v) || isinf(v)) return false;
  out = (float)v;
  return true;
}

bool needNumber(const Command &c, uint8_t argc, char **argv, float &out) {
  if (argc >= 1 && parseNumber(argv[0], out)) return true;
  err(c, F("ARG"), F("expected a number"));
  return false;
}

const char *motorsName(uint8_t m) {
  if (m == motion::MOTOR_A) return "A";
  if (m == motion::MOTOR_B) return "B";
  return "AB";
}

const char *stateName() {
  switch (motion::mode()) {
    case motion::MODE_MOVE: return "MOVE";
    case motion::MODE_RUN:  return "RUN";
    default:                return "IDLE";
  }
}

void printRateRange(const Command &c) {
  errStart(c, F("RANGE"));
  Serial.print(F("rate must be "));
  Serial.print(MIN_RATE_MM_S, 3);
  Serial.print(F(" to "));
  Serial.print(motion::maxRate(), 3);
  Serial.println(F(" mm/s"));
}

// Keep the set rate valid after steps_per_mm changes (the mm/s limit moves).
void reapplyRate() {
  float r = motion::rate();
  if (r > motion::maxRate()) r = motion::maxRate();
  motion::setRate(r);
}

// ----------------------------------------------------------------- handlers --

void cmdHelp(const Command &c, uint8_t, char **) {
  Serial.println(F("# VSBC tensile tester firmware " FIRMWARE_VERSION));
  Serial.println(F("# Commands (then Enter). Aliases in [], + = tension:"));
  Serial.println(F("#   RATE <mm/s>  [V]  crosshead rate for all moves, e.g. v0.5"));
  Serial.println(F("#   MOVE <+-mm>  [M]  relative move, e.g. m5 or m-2"));
  Serial.println(F("#   GOTO <mm>    [G]  absolute move      HOME [H]  go to 0"));
  Serial.println(F("#   RUN <+-mm>   [R]  test move: break / load stops armed"));
  Serial.println(F("#   STOP         [X]  stop. Any other command also stops a move."));
  Serial.println(F("#   ZERO [mm]    [Z]  set the position (default 0) = reference"));
  Serial.println(F("#   TARE         [T]  zero the load cell (no load applied)"));
  Serial.println(F("#   CAL <N>           span calibration with a known load in N"));
  Serial.println(F("#   STATUS [S]  GET  SET <key> <value>  SAVE  DEFAULTS"));
  Serial.println(F("#   STREAM ON|OFF     a data line for every load sample"));
  Serial.println(F("#   MOTORS AB|A|B     service: pulse one driver only"));
  Serial.println(F("#   ID  PING  HELP [?]"));
  Serial.println(F("# Data: D <t_ms> <pos_mm> <load_N> <raw> <I|M|R>"));

  Serial.print(F("# Position "));
  Serial.print(motion::position(), 3);
  Serial.print(motion::referenced() ? F(" mm (referenced)")
                                    : F(" mm (NOT referenced: ZERO at the reference position)"));
  Serial.print(F(", rate "));
  Serial.print(motion::rate(), 4);
  Serial.println(F(" mm/s"));

  Serial.print(F("# Load cell: "));
  if (!tester::loadCellAlive()) {
    Serial.println(F("not responding"));
  } else {
    if (loadCalibrated()) {
      Serial.print(tester::load(), 3);
      Serial.print(F(" N, "));
    } else {
      Serial.print(F("not calibrated, "));
    }
    Serial.print(loadcell::rateHz(), 1);
    Serial.println(F(" samples/s"));
  }
  ok(c);
  Serial.println();
}

void cmdId(const Command &c, uint8_t, char **) {
  ok(c);
  Serial.print(F(" name=" FIRMWARE_NAME " ver=" FIRMWARE_VERSION));
  kvl(F("proto"), PROTOCOL_VERSION);
  kvs(F("loadcell"), loadcell::name());
  Serial.println();
}

void cmdStatus(const Command &c, uint8_t, char **) {
  ok(c);
  kvs(F("state"), stateName());
  kvf(F("pos"), motion::position(), 4);
  kvf(F("load"), tester::load(), 3);
  kvl(F("raw"), tester::raw());
  kvf(F("rate"), motion::rate(), 4);
  kvs(F("motors"), motorsName(motion::motors()));
  kvl(F("ref"), motion::referenced() ? 1 : 0);
  kvl(F("lc"), tester::loadCellAlive() ? 1 : 0);
  kvl(F("cal"), loadCalibrated() ? 1 : 0);
  kvf(F("sps"), loadcell::rateHz(), 1);
  kvl(F("stream"), tester::streaming() ? 1 : 0);
  kvu(F("drops"), tester::droppedLines());
  kvu(F("t"), millis());
  Serial.println();
}

void cmdGet(const Command &c, uint8_t, char **) {
  ok(c);
  settingsPrintAll(Serial);
  Serial.println();
}

void cmdPing(const Command &c, uint8_t, char **) {
  ok(c);
  Serial.println();
}

void cmdStream(const Command &c, uint8_t argc, char **argv) {
  if (argc >= 1) {
    if (strcasecmp(argv[0], "ON") == 0 || strcmp(argv[0], "1") == 0) {
      tester::setStreaming(true);
    } else if (strcasecmp(argv[0], "OFF") == 0 || strcmp(argv[0], "0") == 0) {
      tester::setStreaming(false);
    } else {
      err(c, F("ARG"), F("expected ON or OFF"));
      return;
    }
  }
  ok(c);
  kvl(F("on"), tester::streaming() ? 1 : 0);
  Serial.println();
}

void cmdStop(const Command &c, uint8_t, char **) {
  motion::stop(motion::END_STOP);
  ok(c);
  Serial.println();
}

void cmdRate(const Command &c, uint8_t argc, char **argv) {
  float v;
  if (!needNumber(c, argc, argv, v)) return;
  if (!motion::setRate(v)) {
    printRateRange(c);
    return;
  }
  ok(c);
  kvf(F("rate"), motion::rate(), 4);
  kvf(F("actual"), motion::actualRate(), 4);
  kvf(F("max"), motion::maxRate(), 4);
  Serial.println();
}

void startMove(const Command &c, float target, motion::Mode mode) {
  float from = motion::position();
  int8_t dir = target >= from ? 1 : -1;
  if (tester::estopActive()) {
    err(c, F("ESTOP"), F("E-stop input is active"));
    return;
  }
  if (tester::limitBlocks(dir)) {
    err(c, F("LIMIT"), F("limit switch active in that direction"));
    return;
  }
  // Round to whole steps so the reply shows where the move really ends.
  target = lround(target * settings.steps_per_mm) / settings.steps_per_mm;

  switch (motion::start(target, mode)) {
    case motion::START_OK:
      break;
    case motion::START_LIMIT:
      errStart(c, F("LIMIT"));
      Serial.print(F("target outside the soft limits "));
      Serial.print(settings.min_pos, 3);
      Serial.print(F(" to "));
      Serial.print(settings.max_pos, 3);
      Serial.println(F(" mm"));
      return;
    case motion::START_RATE:
      printRateRange(c);
      return;
    default:
      err(c, F("BUSY"), F("already moving"));
      return;
  }
  tester::beginMove();
  ok(c);
  kvf(F("from"), from, 4);
  kvf(F("target"), target, 4);
  kvl(F("steps"), motion::moveSteps());
  kvf(F("rate"), motion::actualRate(), 4);
  kvf(F("expected"), motion::expectedSeconds(), 2);
  Serial.println();
}

void cmdMove(const Command &c, uint8_t argc, char **argv) {
  float d;
  if (!needNumber(c, argc, argv, d)) return;
  startMove(c, motion::position() + d, motion::MODE_MOVE);
}

void cmdGoto(const Command &c, uint8_t argc, char **argv) {
  float target;
  if (!needNumber(c, argc, argv, target)) return;
  startMove(c, target, motion::MODE_MOVE);
}

void cmdHome(const Command &c, uint8_t, char **) {
  startMove(c, 0.0f, motion::MODE_MOVE);
}

void cmdRun(const Command &c, uint8_t argc, char **argv) {
  float d;
  if (!needNumber(c, argc, argv, d)) return;
  if (d == 0.0f) {
    err(c, F("ARG"), F("distance must not be 0"));
    return;
  }
  if (!tester::loadCellAlive()) {
    err(c, F("NOLOAD"), F("load cell not responding"));
    return;
  }
  if (!loadCalibrated()) {
    err(c, F("UNCAL"), F("load cell not calibrated (TARE, then CAL <N>)"));
    return;
  }
  if (!motion::referenced()) {
    err(c, F("NOREF"), F("position not referenced: ZERO at the reference position first"));
    return;
  }
  if (motion::motors() != motion::MOTORS_AB) {
    err(c, F("ARG"), F("RUN needs both motors (MOTORS AB)"));
    return;
  }
  startMove(c, motion::position() + d, motion::MODE_RUN);
}

void cmdZero(const Command &c, uint8_t argc, char **argv) {
  float v = 0.0f;
  if (argc >= 1 && !parseNumber(argv[0], v)) {
    err(c, F("ARG"), F("expected a number"));
    return;
  }
  motion::setPosition(v);
  ok(c);
  kvf(F("pos"), motion::position(), 4);
  Serial.println();
}

void cmdTare(const Command &c, uint8_t, char **) {
  if (!tester::startAverage(tester::AVG_TARE, 0.0f)) {
    err(c, F("NOLOAD"), F("load cell not responding"));
    return;
  }
  Serial.println(F("# Averaging load-cell samples..."));
}

void cmdCal(const Command &c, uint8_t argc, char **argv) {
  float known;
  if (!needNumber(c, argc, argv, known)) return;
  if (known == 0.0f) {
    err(c, F("ARG"), F("known load must not be 0"));
    return;
  }
  if (!tester::startAverage(tester::AVG_CAL, known)) {
    err(c, F("NOLOAD"), F("load cell not responding"));
    return;
  }
  Serial.println(F("# Averaging load-cell samples..."));
}

void cmdMotors(const Command &c, uint8_t argc, char **argv) {
  uint8_t m = 0;
  if (argc >= 1) {
    if (strcasecmp(argv[0], "AB") == 0 || strcasecmp(argv[0], "BA") == 0) m = motion::MOTORS_AB;
    else if (strcasecmp(argv[0], "A") == 0) m = motion::MOTOR_A;
    else if (strcasecmp(argv[0], "B") == 0) m = motion::MOTOR_B;
  }
  if (m == 0) {
    err(c, F("ARG"), F("expected AB, A or B"));
    return;
  }
  motion::selectMotors(m);
  ok(c);
  kvs(F("motors"), motorsName(motion::motors()));
  Serial.println();
  if (m != motion::MOTORS_AB) {
    Serial.println(F("# Service mode: only one driver gets pulses. A move clears the position reference."));
  }
}

void cmdSet(const Command &c, uint8_t argc, char **argv) {
  if (argc < 2) {
    err(c, F("ARG"), F("usage: SET <key> <value> (GET lists the keys)"));
    return;
  }
  switch (settingsSet(argv[0], argv[1])) {
    case SET_OK:
      break;
    case SET_UNKNOWN:
      err(c, F("ARG"), F("unknown key (GET lists them)"));
      return;
    case SET_BAD_VALUE:
      err(c, F("ARG"), F("expected a number"));
      return;
    case SET_RANGE:
      err(c, F("RANGE"), F("value out of range"));
      return;
  }
  if (strcasecmp(argv[0], "steps_per_mm") == 0) reapplyRate();
  ok(c);
  settingsPrintKey(argv[0], Serial);
  Serial.println();
}

void cmdSave(const Command &c, uint8_t, char **) {
  settingsSave();
  ok(c);
  Serial.println();
}

void cmdDefaults(const Command &c, uint8_t, char **) {
  settingsDefaults();
  reapplyRate();
  ok(c);
  Serial.println();
  Serial.println(F("# Defaults restored in RAM. SAVE to keep them."));
}

const Command COMMANDS[] = {
  {"HELP",     "?",     cmdHelp,     true},
  {"ID",       nullptr, cmdId,       true},
  {"STATUS",   "S",     cmdStatus,   true},
  {"GET",      nullptr, cmdGet,      true},
  {"PING",     nullptr, cmdPing,     true},
  {"STREAM",   nullptr, cmdStream,   true},
  {"STOP",     "X",     cmdStop,     true},
  {"RATE",     "V",     cmdRate,     false},
  {"MOVE",     "M",     cmdMove,     false},
  {"GOTO",     "G",     cmdGoto,     false},
  {"HOME",     "H",     cmdHome,     false},
  {"RUN",      "R",     cmdRun,      false},
  {"ZERO",     "Z",     cmdZero,     false},
  {"TARE",     "T",     cmdTare,     false},
  {"CAL",      nullptr, cmdCal,      false},
  {"MOTORS",   nullptr, cmdMotors,   false},
  {"SET",      nullptr, cmdSet,      false},
  {"SAVE",     nullptr, cmdSave,     false},
  {"DEFAULTS", nullptr, cmdDefaults, false},
};

const Command *findCommand(const char *word) {
  for (const Command &c : COMMANDS) {
    if (strcasecmp(word, c.name) == 0 || (c.alias && strcasecmp(word, c.alias) == 0)) return &c;
  }
  return nullptr;
}

// ---------------------------------------------------------------- parsing --

uint8_t tokenize(char *s, char **tok) {
  uint8_t n = 0;
  while (*s && n < MAX_TOKENS) {
    while (*s == ' ' || *s == '\t') s++;
    if (!*s) break;
    tok[n++] = s;
    while (*s && *s != ' ' && *s != '\t') s++;
    if (*s) *s++ = '\0';
  }
  return n;
}

// "v0.5" -> "v" "0.5", "m-5" -> "m" "-5": a command word directly followed by a number.
void splitCompact(char **tok, uint8_t &n, char *cmd_buf, size_t buf_size) {
  char *s = tok[0];
  if (!isalpha(s[0]) || n >= MAX_TOKENS) return;
  char *p = s;
  while (isalpha(*p)) p++;
  if (!(isdigit(*p) || *p == '-' || *p == '+' || *p == '.')) return;
  size_t len = p - s;
  if (len >= buf_size) return;
  memcpy(cmd_buf, s, len);
  cmd_buf[len] = '\0';
  for (uint8_t i = n; i > 1; i--) tok[i] = tok[i - 1];
  tok[0] = cmd_buf;
  tok[1] = p;
  n++;
}

void handleLine(char *s) {
  tester::hostActivity();
  char *tok[MAX_TOKENS];
  char cmd_buf[12];
  uint8_t n = tokenize(s, tok);
  if (n == 0) return;
  splitCompact(tok, n, cmd_buf, sizeof(cmd_buf));

  const Command *c = findCommand(tok[0]);
  if (!c) {
    if (motion::busy()) motion::stop(motion::END_STOP);
    Serial.print(F("ERR "));
    Serial.print(tok[0]);
    Serial.println(F(" UNKNOWN unknown command (? for help)"));
    return;
  }
  if (!c->any_time) {
    // Any other command during a move stops it, like the old "any key cancels".
    if (motion::busy()) {
      motion::stop(motion::END_STOP);
      err(*c, F("BUSY"), F("motion stopped"));
      return;
    }
    if (tester::averaging()) {
      err(*c, F("BUSY"), F("TARE/CAL averaging in progress"));
      return;
    }
  }
  c->handler(*c, n - 1, tok + 1);
}

void endLine() {
  if (line_overflow) {
    tester::hostActivity();
    if (motion::busy()) motion::stop(motion::END_STOP);
    Serial.println(F("ERR LINE ARG line too long"));
  } else if (line_len > 0) {
    line[line_len] = '\0';
    handleLine(line);
  }
  line_len = 0;
  line_overflow = false;
}

}  // namespace

namespace commands {

void poll() {
  while (Serial.available()) {
    char ch = Serial.read();
    last_char_ms = millis();
    if (ch == '\n' || ch == '\r') {
      endLine();
    } else if (line_len < sizeof(line) - 1) {
      line[line_len++] = ch;
    } else {
      line_overflow = true;
    }
  }
  if ((line_len > 0 || line_overflow) && millis() - last_char_ms >= LINE_IDLE_MS) endLine();
}

}  // namespace commands
