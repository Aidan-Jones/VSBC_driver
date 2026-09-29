/*
  Everything that happens around the step engine: load-cell samples, safety
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
unsigned long droppedLines();

// Starts averaging AVERAGE_SAMPLES samples; the OK/ERR reply is printed when
// done. Returns false (and starts nothing) if the load cell is not responding.
bool startAverage(AvgKind kind, float known_n);
bool averaging();

bool estopActive();
bool limitBlocks(int8_t dir);   // an active limit switch blocks moving this way

char stateChar();         // 'I', 'M' or 'R'

}  // namespace tester
