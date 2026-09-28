# VSBC_driver

Stepper motor control code for the **Variable Stiffness** capstone project (MEEN 402, Texas A&M University).

_TODO: one-paragraph description of the variable stiffness mechanism and what the motors actuate._

This repository contains firmware and scripts that drive two Stepperonline 23HS30-3004S stepper motors, each through its own Stepperonline DM542T V4.0 full digital stepper driver. Each motor turns a 5 mm-lead ball screw that displaces the beam. The primary controller is an **Arduino Mega 2560**. It uses a hardware timer to drive both motors in perfect lockstep, moving the beam a set distance at a constant displacement rate. A single-driver sketch runs the same trials on one driver/motor at a time. An earlier **Raspberry Pi 5** single-motor prototype is also included.

---

## Hardware

| Component | Part | Notes |
|---|---|---|
| Stepper motor (x2) | Stepperonline 23HS30-3004S | NEMA 23, 1.8°/step (200 full steps/rev), 3.0 A/phase |
| Ball screw (x2) | 5 mm lead, coupled directly to the motor | 5 mm per rev → 0.0125 mm per step at 400 pulses/rev |
| Stepper driver (x2, one per motor) | Stepperonline **Full Digital Stepper Driver DM542T, Version 4.0** | 1.0–4.5 A, 18–50 VDC, opto-isolated PUL/DIR/ENA; see [Stepper drivers](#stepper-drivers--stepperonline-dm542t-v40) |
| Controller (primary) | Arduino Mega 2560 | ATmega2560 @ 16 MHz; uses the 16-bit Timer1 compare interrupt |
| Controller (prototype) | Raspberry Pi 5 | Single motor, software-timed |
| Motor power supply | _TODO: voltage / current rating_ | Must be within the DM542T's input range |

## Repository layout

| File | Platform | Status | Description |
|---|---|---|---|
| [`dual_stepper_mega/dual_stepper_mega.ino`](dual_stepper_mega/dual_stepper_mega.ino) | Arduino Mega 2560 | **Current (main)** | Constant-rate displacement trials on **both** motors in lockstep, controlled from the Serial Monitor |
| [`single_driver_test/single_driver_test.ino`](single_driver_test/single_driver_test.ino) | Arduino Mega 2560 | **Current** | Bring-up tests plus the same displacement trials on **one** selected driver/motor |
| [`serial_logger.py`](serial_logger.py) | PC (Python 3) | **Current** | Serial terminal for either Mega sketch that also saves the session to `Outputs/<date>_<time>_<sketch>.log` |
| [`Controller1.0.py`](Controller1.0.py) | Raspberry Pi 5 | Legacy prototype | Single-motor control via `gpiozero`; includes DIP switch notes |

See [Displacement trials](#displacement-trials) for how to set the rate and distance, run a trial and cancel it.

---

## Stepper drivers — Stepperonline DM542T V4.0

There are two drivers, one per motor, both **Stepperonline Full Digital Stepper Driver DM542T, Version 4.0**. V4.0 differs from older DM542T revisions (different microstep table, and a 5V/24V signal selector), so use the [V4.0 user manual](https://www.omc-stepperonline.com/download/DM542T_V4.0.pdf) (Rev 4.0, Oct 2020) and not generic DM542T tables.

### Key specifications (V4.0 manual)

| Parameter | Value |
|---|---|
| Supply voltage | 18–50 VDC (24–48 VDC typical) |
| Output current | 1.0–4.5 A peak (8 settings) |
| Logic signal current | 7–10 mA per input |
| Pulse input frequency | 0–200 kHz |
| Min. pulse width | 2.5 µs (50% duty recommended) |
| Min. DIR setup before PUL | 5 µs |
| Signal levels | High 4.5–5 V (or 24 V, per S2), low 0–0.5 V |
| Enable (ENA) | Not connected by default = driver enabled |

### S2 signal-voltage selector (important)

V4.0 has a 1-bit selector, **S2**, that sets the control signal voltage. **It ships set to 24V.** Both the Arduino Mega (5 V) and the Pi (5 V rail) setups use 5 V signals, so **set S2 to 5V on both drivers**. At the 24V setting, 5 V pulses may not register reliably.

### Auto-configuration

At power-up the V4.0 driver configures itself to match the connected motor. Connect the motor **before** powering the driver, and keep the shaft still for the first moment after power-up.

### DIP switches

| Switches | Function |
|---|---|
| SW1–SW3 | Dynamic (running) current |
| SW4 | Standstill current: OFF = 50%, ON = 90% of the running current. The driver drops to it 0.4 s after the last pulse. |
| SW5–SW8 | Microstep resolution (pulses/rev) |

**Required setting.** **Both drivers must match.**

| SW1 | SW2 | SW3 | SW4 | SW5 | SW6 | SW7 | SW8 |
|---|---|---|---|---|---|---|---|
| OFF | OFF | ON | ON | **OFF** | **ON** | **ON** | **ON** |

According to the V4.0 tables below, this means:
- **Current:** 2.37 A peak / 1.69 A RMS. That is below the motor's 3.0 A rating, so torque is reduced but the motor runs cooler.
- **Standstill:** 90% of running current.
- **Microstep:** **400 pulses/rev**, which matches `STEPS_PER_REV = 400` in the code.

> The earlier setting ("3–4 ON, the rest OFF", with SW5–8 all OFF) is 25000 pulses/rev. With that setting the code turns each motor only about 5.8° per "revolution". Change SW6, SW7 and SW8 to ON.

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
| **400** | **2** | **OFF** | **ON** | **ON** | **ON** ← required (matches code) |
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
> - `STEPS_PER_REV` in the code **must equal** the SW5–SW8 pulses/rev.
> - **Both drivers must have identical DIP and S2 settings.** The Mega sends both drivers the same pulses at the same instant, so a mismatch makes the two motors move different angles.

---

## Motion predictions

Motion depends on the driver's pulses/rev, not on `STEPS_PER_REV` (the code only uses it as the step count for "one revolution").

| Controller | Commanded | Step rate | **Required DIP (400 pulses/rev)** | Old DIP (25000 pulses/rev) |
|---|---|---|---|---|
| Arduino Mega 2560 (single test `f`/`r`) | 400 steps | 500 steps/s (exact) | **1 rev per move**, 75 RPM, 0.8 s | 5.76° per move, 1.2 RPM, 0.8 s |
| Raspberry Pi 5 | `rotate(400)` | ≤ 333 steps/s (sleep-limited) | **1 rev per move**, ≈ 50 RPM, ≥ 1.2 s | ≈ 5.8° per move, ≈ 0.8 RPM, ≥ 1.2 s |

Formulas: `angle = steps / pulses_per_rev × 360°` and `RPM = step_rate / pulses_per_rev × 60`.

To use a finer microstep later (e.g. 1600 or 3200 for smoother motion), change SW5–8 on both drivers and set `STEPS_PER_REV` to match. Raise the step rate by the same factor to keep the same RPM.

Timing margins check out on both controllers. The Mega's pulses are high for 1 ms against a 2.5 µs minimum, and it waits 10 µs after setting DIR against a 5 µs minimum. The Pi's pulses are high for 1.5 ms and it waits 1 ms after setting DIR.

---

## Arduino Mega 2560 — synchronized dual motor (primary)

### Wiring

The drivers connect **directly** to the Mega; no transistors are needed. Each driver has its own STEP and DIR pins, and all four are on the Mega's **PORTA** (double-row header, pins 22–25):

| Driver terminal | Mega pin | Signal | AVR port bit |
|---|---|---|---|
| Driver A PUL+ | D22 | STEP A | PA0 |
| Driver A PUL- | GND | — | — |
| Driver A DIR+ | D24 | DIR A | PA2 |
| Driver A DIR- | GND | — | — |
| Driver B PUL+ | D23 | STEP B | PA1 |
| Driver B PUL- | GND | — | — |
| Driver B DIR+ | D25 | DIR B | PA3 |
| Driver B DIR- | GND | — | — |
| ENA+ / ENA- (both drivers) | not connected | — | — |

```
  Mega D22 (STEP A) ──► Driver A PUL+      Driver A PUL- ──► Mega GND
  Mega D24 (DIR A)  ──► Driver A DIR+      Driver A DIR- ──► Mega GND
  Mega D23 (STEP B) ──► Driver B PUL+      Driver B PUL- ──► Mega GND
  Mega D25 (DIR B)  ──► Driver B DIR+      Driver B DIR- ──► Mega GND
```

- This is **common-cathode** wiring: the Mega pin drives the + input, and the − input goes to Mega GND. Pin HIGH = signal on.
- Each input draws about 7–10 mA, well within the Mega's 20 mA recommended per-pin current (40 mA absolute maximum).
- The PUL- and DIR- wires from both drivers can share any Mega GND pin.
- Set **S2 = 5V** on both drivers. ENA+/ENA- are left unconnected, which keeps the drivers enabled.
- All four signals are on the same AVR port, so the dual sketch toggles both STEP lines with **one register write**. Both drivers see their edges on the same clock cycle. Separate lines per driver let the test sketch pulse one driver on its own.

**Motor & power (per driver):**
- `A+ / A- / B+ / B-` → motor coil leads (_TODO: confirm colors from the 23HS30-3004S datasheet_)
- `+Vdc / GND` → motor power supply

### Setup and upload

1. Install the [Arduino IDE](https://www.arduino.cc/en/software).
2. The Mega uses **Arduino AVR Boards**, which comes with the IDE. No extra libraries are needed.
3. Open `dual_stepper_mega/dual_stepper_mega.ino`. The IDE requires each sketch to sit in a folder with the same name.
4. Select **Tools → Board → Arduino AVR Boards → Arduino Mega or Mega 2560**, set **Processor: ATmega2560 (Mega 2560)**, and pick the correct port.
5. Click **Upload**.
6. Run `python serial_logger.py` to get a terminal **and a log file** (see [Logging](#logging-trials-to-a-file)), or open the IDE's **Serial Monitor** at **115200 baud** (any line ending; no log). Then use the [trial commands](#displacement-trials).

If Timer1 can't be configured at startup, the sketch prints an error and **rapidly blinks the onboard LED** (D13) instead of running.

### Configuration

| Parameter | Location | Default | Meaning |
|---|---|---|---|
| `STEP_A` / `STEP_B` / `DIR_A` / `DIR_B` | top of file | PA0–PA3 (D22–D25) | PORTA bit masks for the output pins |
| `STEPS_PER_REV` | top of file | 400 | Must match SW5–8 on both drivers |
| `SCREW_LEAD_MM` | top of file | 5.0 | Ball screw travel per motor revolution |
| `DEFAULT_RATE_MM_S` / `DEFAULT_DISTANCE_MM` | top of file | 0.5 / 10.0 | Trial settings at power-up (change at run time with `v` / `d`) |
| `MAX_MOTOR_RPM` | top of file | 120 | Motor speed limit; see [Rate limit](#rate-limit). Sets `MAX_RATE_MM_S`. |
| `MIN_RATE_MM_S` / `MAX_RATE_MM_S` | top of file | 0.01 / 10.0 | Allowed rate range. 10 mm/s = 800 steps/s = 120 RPM. |
| `MAX_DISTANCE_MM` | top of file | 100.0 | Largest single trial. **TODO: set to the real beam travel.** |
| `OPEN_DIR_HIGH` | top of file | `true` | DIR level that opens the beam. Flip it if `o` moves the wrong way. |
| `INVERT_B` | top of file | `false` | Set `true` if motor B is mounted mirrored and turns opposite to A |
| `PROGRESS_INTERVAL_MS` | top of file | 500 | How often position is printed during a trial |
| `MAX_STEP_RATE_HZ` | top of file | 20000 | Highest step rate the timer code accepts. The ISR sets this limit, not the driver (200 kHz max). |

### How it works

- `initTimer()` (called once from `setup()`) puts **Timer1** (16-bit) in CTC mode with its clock stopped. Timer0 is left alone because `millis()`/`delay()` use it.
- `setStepRate()` picks the smallest prescaler (1, 8, 64, 256, 1024) for which `OCR1A = F_CPU / (prescaler × 2 × rate) − 1` fits in 16 bits. It also records the rate the timer really produces (`actual_rate_hz`) after rounding. At 0.5 mm/s (40 steps/s) this is prescaler 8 with `OCR1A = 24999`, which gives an exact rate.
- `startMove()` sets both DIR bits and waits 10 µs for direction setup (the V4.0 minimum is 5 µs). It then starts Timer1 and returns straight away.
- On each compare match, `ISR(TIMER1_COMPA_vect)` toggles both STEP bits with `PORTA ^= STEP_MASK`. Every falling edge counts as one completed step. When `steps_remaining` reaches zero, the ISR stops motion and the timer.
- While the ISR steps, `waitForMove()` watches the serial port for a cancel key, prints progress, and enforces a timeout (expected move time + 1 s). On cancel or timeout, `stopMotion()` stops the timer and pulls the STEP lines low. The steps actually made are added to the position.
- Pulse edges come from a hardware timer interrupt rather than software delays, so timing is essentially jitter-free. The worst case is a few µs of delay when the `millis()` or serial interrupt happens to be running.

---

## Displacement trials

A **trial** moves the beam a set distance at a constant displacement rate. The main sketch (`dual_stepper_mega`) moves both ball screws together. The single-driver sketch moves only the selected one.

### Commands (Serial Monitor, 115200 baud)

| Command | Action |
|---|---|
| `v<mm/s>` | Set displacement rate, e.g. `v0.5` (0.01–10 mm/s; see [Rate limit](#rate-limit)) |
| `d<mm>` | Set trial distance, e.g. `d20` (0.0125–100 mm) |
| `o` | Run trial: **open** by the set distance at the set rate |
| `c` | Run trial: **close** by the set distance at the set rate |
| `h` | Return to the zero position at the set rate |
| `z` | Set the current position as zero |
| `?` | Show the menu and current rate, distance and position |
| **any key** during a trial | **Cancel**: the motors stop immediately |

The Serial Monitor sends text only when you press **Enter**, so cancel by typing any character (e.g. `x`) and pressing Enter. Whitespace/newlines alone do not cancel. Invalid values are rejected with the allowed range, and the previous value is kept.

Example session:

```
v0.5        -> Rate set to 0.5000 mm/s
d20         -> Distance set to 20.000 mm (1600 steps)
o           -> OPEN 20.000 mm (1600 steps) at 0.5000 mm/s (40.00 steps/s), expected 40.0 s
               t = 0.5 s   position = 0.250 mm   ... (every 0.5 s)
               Done: moved 20.000 mm in 40.00 s (avg 0.5000 mm/s). Position: 20.000 mm
h           -> CLOSE 20.000 mm back to zero
```

After a cancel, the sketch reports the partial distance, and the position stays correct, so `h` returns to zero.

### Constant rate and the math

- `mm per step = SCREW_LEAD_MM / STEPS_PER_REV = 5 / 400 = 0.0125 mm`
- `step rate (steps/s) = rate (mm/s) / 0.0125`: 0.5 mm/s = 40 steps/s, 1 mm/s = 80 steps/s
- Distances are rounded to whole steps (0.0125 mm). The trial printout shows the exact step count and the actual timer rate.

The rate is set by the hardware timer, not by the load. Each pulse moves the screw exactly one step, so the displacement rate stays constant while the force on the motors changes, **provided the motors do not stall**. The system is open loop, with no encoder or limit switches, so a stall is not detected. Signs of a stall are an elapsed time that doesn't match distance/rate, a buzzing or grinding motor, or a measured displacement that is short. If you see any of these, lower the rate or raise the driver current (SW1–3). There is deliberately **no acceleration ramp**: a ramp would make the start and end of the trial slower than the set rate. At trial speeds (tens to hundreds of steps/s) the motor starts reliably without one.

### Rate limit

The rate is capped at **`MAX_MOTOR_RPM` = 120 RPM**, which is **10 mm/s** (800 steps/s). A request above the cap is rejected with an error, and the previous rate is kept:

```
v15  -> ERROR: 15.0000 mm/s exceeds the motor limit of 10.00 mm/s (120 RPM). Rate unchanged.
```

Why 120 RPM for the 23HS30-3004S (1.89 N·m holding, 4.8 mH, 1.13 Ω, 440 g·cm² rotor):

- **Torque roll-off.** Torque stays near its low-speed value until the driver can no longer push full current through the coil inductance each step. That happens at about `V / (2·L·I)` full steps/s. At 2.37 A peak this is about **320 RPM at 24 V** and **630 RPM at 48 V**. 120 RPM is well under that for any supply in the DM542T's 18–50 V range, so close to full torque is available at every allowed rate.
- **No-ramp start.** Trials start at full rate from standstill, because a ramp would break the constant rate. The motor has to lock onto the rate within the first step while also accelerating the ball screw's inertia. Low speeds give a comfortable margin for that.
- The ISR itself can go much faster (`MAX_STEP_RATE_HZ` = 20000), so the motor, not the code, sets this limit.

If a faster rate is needed and the supply is 36–48 V, `MAX_MOTOR_RPM` can be raised. Test it under the real load first, and check that the elapsed time still matches distance/rate. The limit is an estimate from the published specs; the torque-curve PDF couldn't be read here.

### Logging trials to a file

The Mega has no clock or storage, so logging runs on the PC. [`serial_logger.py`](serial_logger.py) replaces the Arduino Serial Monitor. It shows everything the Mega prints and sends what you type (press Enter). It also writes the whole session to a log file named after the start date, the start time and the sketch that is running:

```
Outputs/2026-09-28_14-30-05_dual_stepper_mega.log
Outputs/2026-09-28_15-02-41_single_driver_test.log
```

Setup (once) and run:

```bash
pip install pyserial
python serial_logger.py              # auto-detects the Mega
python serial_logger.py --port COM5  # or name the port
```

- **Log contents:** every line gets a PC timestamp, and your commands are logged as `> command`. The log starts with a short header giving the sketch, start time and port.
- **Sketch name:** each sketch prints `Sketch: <name>` at startup (`SKETCH_NAME` at the top of the file). Opening the port resets the Mega, so the logger reads that line to name the file. If it doesn't arrive, the logger sends `?` to get the menu; if that fails too, the file is named `unknown_sketch`.
- **Cancel:** works the same as in the Serial Monitor: type `x` and press Enter.
- **Exit:** Ctrl+C or `quit`. On exit the logger sends a cancel, so no trial keeps running after the terminal closes.
- **Port conflict:** only one program can have the port open at a time. Close the Arduino Serial Monitor before running the logger.
- **Not in git:** `Outputs/` and `*.log` are in `.gitignore`, so logs stay local.

Position is counted from power-up (or the last `z`). It is lost on reset, so set zero at a known beam position before each test.

---

## Single-driver test (Arduino Mega 2560)

`single_driver_test/single_driver_test.ino` uses the same wiring and Timer1 step engine as the dual sketch, but pulses **only the selected driver**. The other driver's STEP and DIR stay LOW, so it never moves. Use it to bring up each driver/motor on its own before running both together.

### Single-driver wiring

Wire the driver under test as **Driver A**:

| Driver terminal | Arduino Mega pin |
|---|---|
| PUL+ | D22 |
| PUL- | GND |
| DIR+ | D24 |
| DIR- | GND |
| ENA+ | Not connected |
| ENA- | Not connected |

The motor and the power supply connect only to the driver. Set S2 = 5V and SW1–SW8 = OFF OFF ON ON OFF ON ON ON.

### Upload and use

1. Open `single_driver_test/single_driver_test.ino` and upload it with the same board settings as above.
2. Open **Serial Monitor** at **115200 baud**. Any line ending works.
3. Type a command and press Enter:

| Command | Action |
|---|---|
| `a` / `b` | Select driver A (D22/D24) or B (D23/D25). The default is A. |
| `f` / `r` | One revolution forward / reverse |
| `t` | Back-and-forth test: 1 rev forward, 1 rev back, three times |
| `s<number>` | Set step rate for `f`/`r`/`t` in steps/s, e.g. `s500` (default 500, max 800 = 120 RPM) |
| `v`, `d`, `o`, `c`, `h`, `z` | [Displacement trial](#displacement-trials) commands, acting on the selected driver only |
| `?` | Show the menu and the current driver, rates, distance and position |
| any key during a move | Cancel the move |

After each revolution the sketch prints the driver, direction, step count and elapsed time. At 500 steps/s one revolution takes about 800 ms. Position is tracked separately for driver A and driver B.

### Suggested bring-up order

1. Select `a` and run `f`, `r` and `t`. Confirm that only motor A turns, exactly one revolution each way.
2. Run a short trial: `v1`, `d5`, `o`. Expect 400 steps (one revolution) in about 5.0 s at 1.0000 mm/s. Measure the screw/beam travel with calipers: it should be 5 mm. Check that `o` opens; if it closes, flip `OPEN_DIR_HIGH`.
3. Test cancel: `v0.5`, `d20`, `o`, then send `x` mid-trial. The screw should stop at once. Then send `h`, which should return it to zero.
4. Select `b` and repeat. If B moves the opposite way to A, set `INVERT_B = true` (in both sketches).
5. Upload `dual_stepper_mega` and confirm that both screws move together, the same distance and in the same direction.

---

## Raspberry Pi 5 — single motor (prototype)

### Wiring (BCM numbering)

| Driver pin | Connects to |
|---|---|
| PUL+ | Pi 5V |
| PUL- | GPIO 20 |
| DIR+ | Pi 5V |
| DIR- | GPIO 21 |

Set **S2 = 5V** on the driver. This direct 3.3V GPIO wiring is marginal on the DM542T V4.0; see [Known limitations](#known-limitations--todo).

### Setup and run

`gpiozero` is preinstalled on Raspberry Pi OS. If needed:

```bash
sudo apt install python3-gpiozero
```

Run:

```bash
python3 Controller1.0.py
```

The script turns one revolution clockwise, pauses 0.5 s, then one revolution counter-clockwise. Press **Ctrl+C** to stop. The GPIO pins are driven low and released on exit.

### Configuration

| Parameter | Default | Meaning |
|---|---|---|
| `STEP_PIN` / `DIR_PIN` | 20 / 21 | BCM GPIO numbers |
| `STEPS_PER_REV` | 400 | Must match SW5–8 |
| `PULSE_DELAY` | 0.0015 s | Delay per pulse edge. One step takes at least 2 × delay, so the rate is at most 333 steps/s: about 50 RPM at 400 pulses/rev. `sleep()` overhead makes the real rate a little lower. |

`rotate(steps, clockwise=True, delay=PULSE_DELAY)` performs a blocking move.

### Limitations of the Pi approach

Pulses are timed with Python's `sleep()`, so Linux scheduling introduces jitter. This is acceptable for a single motor at low speed but is not suitable for precise or synchronized multi-motor motion — which is why the project moved to an Arduino (now the Mega 2560). This script is kept as a legacy reference and has not been updated for the Mega.

---

## Known limitations / TODO

- [ ] **Hardware: set SW5–8 to OFF ON ON ON (400 pulses/rev) on both drivers.** The old all-OFF setting is 25000 pulses/rev.
- [ ] **Hardware: set S2 to 5V on both drivers.** It ships at 24V, and all the control signals here are 5 V.
- [ ] **Current is below the motor rating.** SW1–3 at OFF OFF ON gives 2.37 A peak / 1.69 A RMS, while the motor is rated 3.0 A. Raise it if you need more torque.
- [ ] **Pi logic-level issue.** With PUL+/DIR+ tied to 5V and PUL-/DIR- driven by 3.3V GPIO, the input sees about 1.7 V when the pin is "high". The V4.0 spec needs 0–0.5 V for low and 4.5–5 V for high, so 1.7 V is in neither range and the driver may miss or add pulses. The signals are also active-low. Use a transistor buffer or a 3.3→5 V level shifter on each line.
- [ ] **No acceleration/deceleration ramps.** This is deliberate, to keep the trial rate constant. The motors start and stop at the trial rate, so very high rates or loads can stall them. `MAX_RATE_MM_S` limits this.
- [ ] **Open loop, no limit switches.** Stalls are not detected, and nothing stops the screw at the end of its travel. Set `MAX_DISTANCE_MM` to the real beam travel, zero (`z`) at a known position, and keep an eye on the first trials.
- [ ] **Hardware: rewire for the Mega.** Each driver now has its own STEP/DIR lines on D22–D25, wired directly to PUL+/DIR+ with PUL-/DIR- to GND (no transistors).
- [ ] **Compile-check on hardware.** The Mega sketches have not been compiled or run on a board yet.
- [x] ~~Arduino: blocking move~~: fixed; moves run from the timer ISR while the main loop watches for the cancel key and prints progress.
- [x] ~~No application logic~~: constant-rate displacement trials added to both Mega sketches.
- [x] ~~Repo cleanup (` (1)` in the `.ino` filename, sketch folders, Python entries in `.gitignore`)~~: done.
- [x] ~~Uno R4 `FspTimer` timer re-created on every move (hang after a few moves)~~: no longer applies; the Mega sketch uses Timer1, configured once in `setup()`.
- [x] ~~Arduino ignored timer setup failures~~: fixed; setup failures now halt with an error message and a blinking LED, and moves time out.
- [x] ~~Duplicate `stepper_control (1).py`~~: removed.

---

## Team

MEEN 402 — Texas A&M University

_TODO: team members, advisor, semester._
