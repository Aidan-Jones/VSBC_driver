/*
  HX711 24-bit load-cell ADC.

  The HX711 pulls DOUT low when a conversion is ready (10 or 80 per second,
  set by its RATE pin -- most boards ship at 10). poll() is called from loop()
  and only reads when DOUT is low, so it never waits.

  Interrupts stay ENABLED while clocking the bits out. The HX711 powers down if
  SCK stays high for more than 60 us; each high pulse here lasts about 5-15 us,
  and even if the Timer1 step ISR and the serial ISRs all preempt one pulse it
  stays well under that. Keeping interrupts on means step timing is untouched.
*/
#include "config.h"

#if LOADCELL_TYPE == LOADCELL_HX711
#include "loadcell.h"

namespace {

const long HX711_MAX = 8388607L;    // 0x7FFFFF: positive full scale
const long HX711_MIN = -8388608L;   // 0x800000: negative full scale
// The HX711 converts at most 80 times a second (12.5 ms). A faster "ready"
// means the chip is unpowered or miswired, so it is ignored.
const unsigned long MIN_SAMPLE_INTERVAL_MS = 5;

unsigned long last_read_ms = 0;
unsigned long last_sample_ms = 0;
bool have_sample = false;
float rate_hz = 0.0f;

void pulse() {
  digitalWrite(HX711_SCK_PIN, HIGH);
  delayMicroseconds(1);
  digitalWrite(HX711_SCK_PIN, LOW);
  delayMicroseconds(1);
}

}  // namespace

namespace loadcell {

void begin() {
  pinMode(HX711_SCK_PIN, OUTPUT);
  digitalWrite(HX711_SCK_PIN, LOW);         // SCK high for > 60 us = power down
  pinMode(HX711_DOUT_PIN, INPUT_PULLUP);    // no HX711 -> reads HIGH -> never ready
}

bool poll(long &raw) {
  if (digitalRead(HX711_DOUT_PIN) != LOW) return false;   // conversion not ready
  unsigned long now = millis();
  if (now - last_read_ms < MIN_SAMPLE_INTERVAL_MS) return false;
  last_read_ms = now;

  uint32_t value = 0;
  for (uint8_t i = 0; i < 24; i++) {
    digitalWrite(HX711_SCK_PIN, HIGH);
    delayMicroseconds(1);
    value = (value << 1) | (digitalRead(HX711_DOUT_PIN) == HIGH ? 1 : 0);
    digitalWrite(HX711_SCK_PIN, LOW);
    delayMicroseconds(1);
  }
  for (uint8_t i = 0; i < HX711_GAIN_PULSES; i++) pulse();

  // After the gain pulses the HX711 drives DOUT high until the next
  // conversion. Still low = the chip did not respond: discard the reading.
  if (digitalRead(HX711_DOUT_PIN) == LOW) return false;

  if (value & 0x800000UL) value |= 0xFF000000UL;   // sign-extend 24-bit two's complement
  raw = (long)value;

  if (have_sample) {
    unsigned long dt = now - last_sample_ms;
    if (dt > 0) {
      float inst = 1000.0f / dt;
      rate_hz = (rate_hz == 0.0f) ? inst : rate_hz + 0.1f * (inst - rate_hz);
    }
  }
  last_sample_ms = now;
  have_sample = true;
  return true;
}

bool alive() {
  return have_sample && millis() - last_sample_ms <= LOADCELL_TIMEOUT_MS;
}

float rateHz() {
  return alive() ? rate_hz : 0.0f;
}

bool saturated(long raw) {
  return raw >= HX711_MAX || raw <= HX711_MIN;
}

const char *name() {
  return "HX711";
}

}  // namespace loadcell

#endif  // LOADCELL_TYPE == LOADCELL_HX711
