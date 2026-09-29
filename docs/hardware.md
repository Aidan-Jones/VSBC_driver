# Hardware reference

Drivers, wiring, and the motor and load-cell details behind the firmware.
The [README](../README.md) covers setup and use.

## Parts

| Component | Part | Notes |
|---|---|---|
| Stepper motor (×2) | Stepperonline 23HS30-3004S | NEMA 23, 1.8°/step (200 full steps/rev), 3.0 A/phase, 1.89 N·m holding, 4.8 mH, 1.13 Ω, 440 g·cm² rotor |
| Ball screw (×2) | 5 mm lead, coupled directly to the motor | 5 mm/rev → 0.0125 mm per step at 400 pulses/rev |
| Stepper driver (×2) | Stepperonline Full Digital Stepper Driver DM542T, **Version 4.0** | 1.0–4.5 A, 18–50 VDC, opto-isolated PUL/DIR/ENA |
| Controller | Arduino Mega 2560 | ATmega2560 @ 16 MHz; Timer1 compare interrupt for stepping |
| Load cell amplifier | HX711 | 24-bit, 10 or 80 samples/s (RATE pin) |
| Host | Raspberry Pi 5 | USB to the Mega |
| Motor power supply | _TODO: voltage / current rating_ | Must be within the DM542T's 18–50 VDC |

---

## Stepper drivers: Stepperonline DM542T V4.0

There are two drivers, one per motor. V4.0 differs from older DM542T revisions: it has a different microstep table and a 5V/24V signal selector. Use the [V4.0 user manual](https://www.omc-stepperonline.com/download/DM542T_V4.0.pdf) (Rev 4.0, Oct 2020), not generic DM542T tables.

### Key specifications (V4.0 manual)

| Parameter | Value |
|---|---|
| Supply voltage | 18–50 VDC (24–48 VDC typical) |
| Output current | 1.0–4.5 A peak (8 settings) |
| Logic signal current | 7–10 mA per input |
| Pulse input frequency | 0–200 kHz |
| Min. pulse width | 2.5 µs (50 % duty recommended) |
| Min. DIR setup before PUL | 5 µs |
| Signal levels | High 4.5–5 V (or 24 V, per S2), low 0–0.5 V |
| Enable (ENA) | Not connected by default = driver enabled |

### S2 signal-voltage selector (important)

S2 sets the control signal voltage. **It ships set to 24V.** The Mega drives 5 V signals, so **set S2 to 5V on both drivers.** At the 24V setting, 5 V pulses may not register reliably.

### Auto-configuration

At power-up the V4.0 driver configures itself to match the connected motor. Connect the motor **before** powering the driver, and keep the shaft still for the first moment after power-up.

### DIP switches

| Switches | Function |
|---|---|
| SW1–SW3 | Dynamic (running) current |
| SW4 | Standstill current: OFF = 50 %, ON = 90 % of the running current. The driver drops to it 0.4 s after the last pulse. |
| SW5–SW8 | Microstep resolution (pulses/rev) |

**Required setting. Both drivers must match.**

| SW1 | SW2 | SW3 | SW4 | SW5 | SW6 | SW7 | SW8 |
|---|---|---|---|---|---|---|---|
| OFF | OFF | ON | ON | **OFF** | **ON** | **ON** | **ON** |

According to the V4.0 tables below, this means:

- **Current:** 2.37 A peak / 1.69 A RMS. That is below the motor's 3.0 A rating, so torque is reduced but the motor runs cooler.
- **Standstill:** 90 % of running current.
- **Microstep:** **400 pulses/rev**, which matches `STEPS_PER_REV = 400` in `config.h`.

> The earlier setting ("3–4 ON, the rest OFF", with SW5–8 all OFF) is 25000 pulses/rev. With it, each "revolution" of code turns the motor only about 5.8°. The travel calibration recognises this (measured/commanded ≈ 0.016) and warns instead of calibrating it away.

#### Output current (SW1–SW3)

| Peak | RMS | SW1 | SW2 | SW3 |
|---|---|---|---|---|
| 1.00 A | 0.71 A | ON | ON | ON |
| 1.46 A | 1.04 A | OFF | ON | ON |
| 1.91 A | 1.36 A | ON | OFF | ON |
| **2.37 A** | **1.69 A** | **OFF** | **OFF** | **ON** ← current |
| 2.84 A | 2.03 A | ON | ON | OFF |
| 3.31 A | 2.36 A | OFF | ON | OFF |
| 3.76 A | 2.69 A | ON | OFF | OFF |
| 4.50 A | 3.20 A | OFF | OFF | OFF |

For the 3.0 A 23HS30-3004S, the 3.31 A or 3.76 A peak rows are closer to the motor's rating if more torque is needed.

#### Microstep resolution (SW5–SW8)

| Pulses/rev | Microstep | SW5 | SW6 | SW7 | SW8 |
|---|---|---|---|---|---|
| 200 | 1 | ON | ON | ON | ON |
| **400** | **2** | **OFF** | **ON** | **ON** | **ON** ← required (matches `config.h`) |
| 800 | 4 | ON | OFF | ON | ON |
| 1600 | 8 | OFF | OFF | ON | ON |
| 3200 | 16 | ON | ON | OFF | ON |
| 6400 | 32 | OFF | ON | OFF | ON |
| 12800 | 64 | ON | OFF | OFF | ON |
| 25600 | 128 | OFF | OFF | OFF | ON |
| 1000 | 5 | ON | ON | ON | OFF |
| 2000 | 10 | OFF | ON | ON | OFF |
| 4000 | 20 | ON | OFF | ON | OFF |
| 5000 | 25 | OFF | OFF | ON | OFF |
| 8000 | 40 | ON | ON | OFF | OFF |
| 10000 | 50 | OFF | ON | OFF | OFF |
| 20000 | 100 | ON | OFF | OFF | OFF |
| 25000 | 125 | OFF | OFF | OFF | OFF ← old setting |

> The rows with SW8 = ON are confirmed from the manual. The rows with SW8 = OFF follow the manual's binary switch pattern. Check both against the label printed on your driver.

> **Rules:**
> - `STEPS_PER_REV` in `firmware/vsbc_firmware/config.h` **must equal** the SW5–SW8 pulses/rev. To use a finer microstep (e.g. 1600 for smoother motion), change SW5–8 on both drivers **and** `STEPS_PER_REV`, then upload again. The nominal steps/mm and the speed cap follow automatically; redo the travel calibration.
> - **Both drivers must have identical DIP and S2 settings.** The Mega sends both drivers the same pulses at the same instant, so a mismatch makes the two motors move different distances.

---

## Wiring

### Mega ↔ drivers

The drivers connect **directly** to the Mega; no transistors are needed. Each driver has its own STEP and DIR pin, and all four are on the Mega's **PORTA** (double-row header, pins 22–25):

| Driver terminal | Mega pin | Signal | AVR port bit |
|---|---|---|---|
| Driver A PUL+ | D22 | STEP A | PA0 |
| Driver A PUL− | GND | — | — |
| Driver A DIR+ | D24 | DIR A | PA2 |
| Driver A DIR− | GND | — | — |
| Driver B PUL+ | D23 | STEP B | PA1 |
| Driver B PUL− | GND | — | — |
| Driver B DIR+ | D25 | DIR B | PA3 |
| Driver B DIR− | GND | — | — |
| ENA+ / ENA− (both drivers) | not connected | — | — |

```
  Mega D22 (STEP A) ──► Driver A PUL+      Driver A PUL- ──► Mega GND
  Mega D24 (DIR A)  ──► Driver A DIR+      Driver A DIR- ──► Mega GND
  Mega D23 (STEP B) ──► Driver B PUL+      Driver B PUL- ──► Mega GND
  Mega D25 (DIR B)  ──► Driver B DIR+      Driver B DIR- ──► Mega GND
```

- This is **common-cathode** wiring: the Mega pin drives the + input, and the − input goes to Mega GND. Pin HIGH = signal on.
- Each input draws about 7–10 mA, well within the Mega's 20 mA recommended per-pin current (40 mA absolute maximum).
- The PUL− and DIR− wires from both drivers can share any Mega GND pin.
- All four signals are on the same AVR port, so the firmware toggles both STEP lines with **one register write**. Both drivers see their edges on the same clock cycle. Separate lines per driver let the service mode (`MOTORS A` / `MOTORS B`) pulse one driver on its own.

**Motor and power (per driver):**
- `A+ / A- / B+ / B-` → motor coil leads (_TODO: confirm colours from the 23HS30-3004S datasheet_)
- `+Vdc / GND` → motor power supply

### Mega ↔ HX711 ↔ load cell

| From | To | Notes |
|---|---|---|
| HX711 VCC | Mega 5V | Some boards have separate VCC (analog) and VDD (logic): both to 5V |
| HX711 GND | Mega GND | |
| HX711 DT / DOUT | Mega D26 | `HX711_DOUT_PIN` in `config.h` |
| HX711 SCK | Mega D27 | `HX711_SCK_PIN` in `config.h` |
| HX711 E+ / E− | Load cell excitation + / − | Usually red / black |
| HX711 A+ / A− | Load cell signal + / − | Usually green / white. Channel A, gain 128. |

- **Sample rate.** The HX711 converts 10 or 80 times a second, set by its RATE pin. Most cheap boards tie RATE to GND (10/s); SparkFun-style boards have a jumper. The firmware reacts to a break or overload within a few samples: a few tens of ms at 80/s, a few hundred at 10/s. `STATUS` reports the measured rate (`sps=`).
- Keep the load-cell wires short and twisted, and away from the motor cables.
- DT is read with the internal pull-up enabled. If the HX711 is missing, DT stays high, the firmware reports "load cell not responding", and manual moves still work.

### Pi ↔ Mega

A USB cable (USB-B on the Mega). The Pi sees it as `/dev/ttyACM0` (genuine Mega) or `/dev/ttyUSB0` (CH340 clones), and the app finds it automatically. The Mega resets whenever the port is opened: it takes about 2 s to boot, and the step position is lost (see "Position reference" in the README).

---

## Motion maths

- `mm per step = SCREW_LEAD_MM / STEPS_PER_REV = 5 / 400 = 0.0125 mm`, i.e. 80 steps/mm nominal. The calibrated value is the EEPROM setting `steps_per_mm`.
- `step rate (steps/s) = rate (mm/s) × steps_per_mm`: 0.5 mm/s = 40 steps/s; 5 mm/min = 0.0833 mm/s = 6.67 steps/s.
- `RPM = step rate / pulses_per_rev × 60`.
- Distances are rounded to whole steps. Every move's reply shows the exact step count and the rate the timer really produces (`actual=`).

The rate is set by the hardware timer, not by the load. Each pulse moves the screw exactly one step, so the displacement rate stays constant while the force on the motors changes, **provided the motors do not stall** (open loop).

### Speed limit: 120 RPM (10 mm/s)

`MAX_MOTOR_RPM = 120` caps every move at 10 mm/s (800 steps/s). A faster request is refused with `ERR ... RANGE`.

Why 120 RPM for the 23HS30-3004S:

- **Torque roll-off.** Torque stays near its low-speed value until the driver can no longer push full current through the coil inductance each step. That happens at about `V / (2·L·I)` full steps/s. At 2.37 A peak this is about **320 RPM at 24 V** and **630 RPM at 48 V**. 120 RPM is well under that for any supply in the DM542T's 18–50 V range, so close to full torque is available at every allowed rate.
- **No-ramp start.** Moves start at full rate from standstill, because a ramp would break the constant rate. The motor has to lock onto the rate within the first step while also accelerating the ball screw's inertia. Low speeds give a comfortable margin for that.
- The ISR itself can go much faster (20000 steps/s), so the motor, not the code, sets this limit.

With a 36–48 V supply, `MAX_MOTOR_RPM` can be raised. Test under the real load first, and check that the elapsed time still matches distance ÷ rate. The limit is an estimate from the published specs.

The slowest rate is 0.002 mm/s (0.12 mm/min). Timer1's floor is about 0.12 steps/s.

---

## How the firmware works

- **Timer1 step engine (`motion.cpp`).** Timer1 (16-bit) runs in CTC mode. For each rate the firmware picks the smallest prescaler (1, 8, 64, 256, 1024) for which `OCR1A = F_CPU / (prescaler × 2 × rate) − 1` fits in 16 bits, and records the rate the timer really produces. Each compare match toggles the selected STEP bits with `PORTA ^= mask`; every falling edge counts as one step. When the step count reaches zero the ISR stops the timer. Timer0 is left alone for `millis()`.
- **Direction.** DIR is set, then the firmware waits 10 µs (V4.0 minimum 5 µs) before the first pulse. Pulses are high for half the step period: 625 µs at the 800 steps/s maximum, far above the 2.5 µs minimum.
- **Non-blocking loop.** `loop()` reads serial commands, polls the HX711, runs the safety checks, reports finished moves and sends data lines. Nothing in it waits for a move to finish, so the load cell is sampled and the safety checks run throughout every move.
- **HX711 reading (`loadcell_hx711.cpp`).** It reads only when DOUT is low (conversion ready), clocking 25 pulses with interrupts **left on**. Each SCK-high pulse lasts about 5–15 µs even when the Timer1 or serial interrupts preempt it; the HX711 only powers down after 60 µs high. So step timing is untouched, and a reading is never corrupted by the step interrupt. After the 25th pulse DOUT must go high again; if it doesn't, the reading is discarded.
- **Safety (`tester.cpp`).** Each sample is checked, using the median of the last three samples so one bad reading can't trigger a stop:
  - overload (only while |load| is rising, so you can back out of an overload);
  - stop load and break detection (test moves only);
  - load cell lost;
  - host watchdog.
- **Telemetry.** A data line is skipped, never delayed, if the serial transmit buffer is full, so the safety loop can't stall behind the USB link.
- **EEPROM (`settings.cpp`).** A struct with a magic number, layout version and CRC. If the layout changes (new firmware) or the data is corrupt, the firmware falls back to defaults and says so at startup.
- **Setup failure.** If Timer1 can't be configured, the firmware prints an error and rapidly blinks the onboard LED (D13) instead of running.

---

## Why not drive the steppers from the Pi?

An earlier prototype stepped one motor straight from the Pi's GPIO with Python `sleep()` timing (still in the git history as `Controller1.0.py`). It had two problems:

- **Timing.** Linux scheduling adds jitter, so a constant rate and synchronised motors are not possible. Reading an HX711 from Linux has the same timing problem.
- **Logic levels.** With PUL+/DIR+ tied to 5 V and PUL−/DIR− driven by 3.3 V GPIO, the driver input sees about 1.7 V when the pin is "high". That is outside both the V4.0's low (0–0.5 V) and high (4.5–5 V) ranges.

The Mega does the timing-critical work, and the Pi only sends commands.
