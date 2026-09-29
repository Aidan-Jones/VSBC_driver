/*
  VSBC tensile tester firmware -- Arduino Mega 2560 (ATmega2560, 16 MHz)

  Drives two DM542T V4.0 stepper drivers in lockstep (Timer1 compare ISR,
  jitter-free, constant displacement rate), reads an HX711 load cell, and
  stops on its own on overload, specimen break, soft travel limits or a silent
  host. It talks a line protocol (docs/protocol.md) that the Raspberry Pi app
  uses and that is also readable in the Arduino Serial Monitor.

  Arduino IDE: open this file, board "Arduino Mega or Mega 2560", upload.
  No extra libraries are needed. Serial Monitor at 115200 baud; type ? + Enter.

  Files in this sketch:
    config.h            pins, mechanics, speed limits, optional switch inputs
    motion.*            Timer1 step engine
    loadcell.h          load-cell interface; loadcell_hx711.cpp implements it
    tester.*            safety stops, TARE/CAL averaging, telemetry, watchdog
    commands.*          command parser and handlers
    settings.*          EEPROM-backed calibration and limits
*/
#include "config.h"
#include "commands.h"
#include "loadcell.h"
#include "motion.h"
#include "settings.h"
#include "tester.h"

// Stop here and blink the LED so a setup failure is obvious instead of a silent hang.
void fatalError(const __FlashStringHelper *msg) {
  Serial.println(msg);
  pinMode(LED_BUILTIN, OUTPUT);
  while (true) {
    digitalWrite(LED_BUILTIN, HIGH);
    delay(100);
    digitalWrite(LED_BUILTIN, LOW);
    delay(100);
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  bool loaded = settingsLoad();
  loadcell::begin();
  tester::begin();
  if (!motion::begin()) fatalError(F("ERR BOOT RANGE could not configure Timer1"));

  Serial.println();
  Serial.println(F("# VSBC tensile tester firmware " FIRMWARE_VERSION));
  if (!loaded) {
    Serial.println(F("# No saved settings (or an older layout): using defaults. Calibrate, then SAVE."));
  }
  Serial.println(F("# Type ? then Enter for the command list."));
  Serial.print(F("EVT READY name=" FIRMWARE_NAME " ver=" FIRMWARE_VERSION " proto="));
  Serial.println(PROTOCOL_VERSION);
}

void loop() {
  commands::poll();
  tester::poll();
}
