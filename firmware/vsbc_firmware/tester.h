/*
  Everything that happens around the step engine: load-cell samples (sent by
  the host or read from the Mega's own HX711, see params.lc_host), safety
  stops, TARE/CAL averaging, the host watchdog, optional switch inputs and the
  D / EVT lines sent to the host.
*/
#pragma once
#include <Arduino.h>

namespace tester {

enum AvgKind : uint8_t { AVG_NONE, AVG_TARE, AVG_CAL };

void begin();
void poll();              // call every loop()
void hostActivity();      // a line arrived from the host
void beginMove();         // call right after motion::start() succeeds

bool streaming();
void setStreaming(bool on);

float load();             // latest load in N (NAN if unknown)
long raw();               // latest raw ADC counts
bool loadCellAlive();
float sampleRateHz();     // measured sample rate of the active source (0 if not responding)
const char *sourceName(); // "HOST", or the name of the Mega's ADC

// A reading sent by the host (LC <raw>). Ignored unless params.lc_host is set.
void hostSample(long raw);
// Call after params.lc_host changes: forgets the samples of the old source.
void sourceChanged();
unsigned long droppedLines();

// Starts averaging the samples of AVERAGE_MS (at least AVERAGE_MIN_SAMPLES);
// the OK/ERR reply is printed when done. Returns false (and starts nothing) if
// the load cell is not responding.
bool startAverage(AvgKind kind, float known_n);
bool averaging();

bool estopActive();
bool limitBlocks(int8_t dir);   // an active limit switch blocks moving this way

char stateChar();         // 'I', 'M' or 'R'

}  // namespace tester
