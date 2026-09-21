# VSBC_driver

Stepper motor control code for the **Variable Stiffness** capstone project (MEEN 402, Texas A&M University).

_TODO: one-paragraph description of the variable stiffness mechanism and what the motors actuate._

This repository contains firmware and scripts that drive two Stepperonline 23HS30-3004S stepper motors, each through its own Stepperonline DM542T V4.0 full digital stepper driver. The primary controller is an **Arduino Uno R4** that drives two motors in perfect lockstep using a hardware timer; an earlier **Raspberry Pi 5** single-motor prototype is also included.

---

## Hardware

| Component | Part | Notes |
|---|---|---|
| Stepper motor (x2) | Stepperonline 23HS30-3004S | NEMA 23, 1.8°/step (200 full steps/rev), 3.0 A/phase |
| Stepper driver (x2, one per motor) | Stepperonline **Full Digital Stepper Driver DM542T, Version 4.0** | 1.0–4.5 A, 18–50 VDC, opto-isolated PUL/DIR/ENA; see [Stepper drivers](#stepper-drivers--stepperonline-dm542t-v40) |
| Controller (primary) | Arduino Uno R4 Minima or WiFi | Renesas RA4M1; uses `FspTimer` hardware timers |
| Controller (prototype) | Raspberry Pi 5 | Single motor, software-timed |
| Signal buffers | NPN transistors (x2) | One for STEP, one for DIR (Arduino setup) |
| Motor power supply | _TODO: voltage / current rating_ | Must be within the DM542T's input range |

## Repository layout

| File | Platform | Status | Description |
|---|---|---|---|
| [`dual_stepper_uno_r4 (1).ino`](dual_stepper_uno_r4%20(1).ino) | Arduino Uno R4 | **Current** | Synchronized dual-motor control via hardware timer interrupt |
| [`Controller1.0.py`](Controller1.0.py) | Raspberry Pi 5 | Prototype | Single-motor control via `gpiozero`; includes DIP switch notes |

Both programs are **bring-up / test programs**: each sends one revolution forward and one back to confirm that the controller, drivers and motors are wired correctly. Neither one contains application logic for the variable stiffness mechanism yet.

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

V4.0 has a 1-bit selector, **S2**, that sets the control signal voltage. **It ships set to 24V.** Both the Arduino (5 V) and the Pi (5 V rail) setups use 5 V signals, so **set S2 to 5V on both drivers**. At the 24V setting, 5 V pulses may not register reliably.

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
> - **Both drivers must have identical DIP and S2 settings.** They share one STEP line, so a mismatch makes the two motors move different angles.

---

## Motion predictions

Motion depends on the driver's pulses/rev, not on `STEPS_PER_REV` (the code only uses it as the step count for "one revolution").

| Controller | Commanded | Step rate | **Required DIP (400 pulses/rev)** | Old DIP (25000 pulses/rev) |
|---|---|---|---|---|
| Arduino Uno R4 | `moveSteps(400, …, 500)` | 500 steps/s (exact) | **1 rev per move**, 75 RPM, 0.8 s | 5.76° per move, 1.2 RPM, 0.8 s |
| Raspberry Pi 5 | `rotate(400)` | ≤ 333 steps/s (sleep-limited) | **1 rev per move**, ≈ 50 RPM, ≥ 1.2 s | ≈ 5.8° per move, ≈ 0.8 RPM, ≥ 1.2 s |

Formulas: `angle = steps / pulses_per_rev × 360°` and `RPM = step_rate / pulses_per_rev × 60`.

To use a finer microstep later (e.g. 1600 or 3200 for smoother motion), change SW5–8 on both drivers and set `STEPS_PER_REV` to match. Raise the step rate by the same factor to keep the same RPM.

Timing margins check out on both controllers. The Arduino's pulses are high for 1 ms against a 2.5 µs minimum, and it waits 10 µs after setting DIR against a 5 µs minimum. The Pi's pulses are high for 1.5 ms and it waits 1 ms after setting DIR.

---

## Arduino Uno R4 — synchronized dual motor (primary)

### Wiring

```
  +5V ──► Driver A PUL+, DIR+
  +5V ──► Driver B PUL+, DIR+

  Uno D4 (STEP) ──► NPN buffer ──┬──► Driver A PUL-
                                 └──► Driver B PUL-

  Uno D5 (DIR)  ──► NPN buffer ──┬──► Driver A DIR-
                                 └──► Driver B DIR-
```

- `STEP_PIN = 4`, `DIR_PIN = 5`
- The NPN buffers sink the drivers' opto-coupler current, so the Arduino pin never has to source it directly. Each buffer drives two inputs at 7–10 mA each, so it sinks about 14–20 mA total.
- Tie Arduino GND to the NPN emitters (common signal ground).
- Set **S2 = 5V** on both drivers. ENA+/ENA- are left unconnected, which keeps the drivers enabled.

**Motor & power (per driver):**
- `A+ / A- / B+ / B-` → motor coil leads (_TODO: confirm colors from the 23HS30-3004S datasheet_)
- `+Vdc / GND` → motor power supply

### Setup and upload

1. Install the [Arduino IDE](https://www.arduino.cc/en/software).
2. In **Boards Manager**, install **Arduino UNO R4 Boards** (provides `FspTimer.h`; no extra library needed).
3. Open `dual_stepper_uno_r4 (1).ino`.
4. Select **Tools → Board → Arduino UNO R4 Minima** (or WiFi) and the correct port.
5. Click **Upload**.

6. Optional: open **Serial Monitor** at 115200 baud to see any error messages.

The demo `loop()` turns both motors one revolution forward at 500 steps/s (75 RPM), pauses 0.5 s, turns one revolution back, pauses, and repeats.

If no hardware timer is available at startup, the sketch prints an error and **rapidly blinks the onboard LED** instead of running.

### Configuration

| Parameter | Location | Default | Meaning |
|---|---|---|---|
| `STEP_PIN` / `DIR_PIN` | top of file | 4 / 5 | Output pins |
| `STEPS_PER_REV` | top of file | 400 | Must match SW5–8 on both drivers |
| `STEP_RATE_HZ` | top of file | 500 | Step rate in steps/s used by `loop()`. RPM = rate / pulses_per_rev × 60 = 75 RPM at 400 pulses/rev. Driver max is 200 kHz. |

Use `moveSteps(steps, clockwise, step_rate_hz)` to command a move. It blocks until the move completes, and returns `false` if the rate couldn't be set or the move timed out.

### How it works

- `initTimer()` (called once from `setup()`) claims a GPT hardware timer and attaches the interrupt. The timer is configured once and reused because the R4 core's `FspTimer::close()` does not free the timer channel. Re-creating the timer for every move, as the original code did, would run out of timers after a few moves and hang.
- `moveSteps()` sets the DIR pin, waits 10 µs for direction setup (V4.0 minimum is 5 µs), updates the timer frequency to **2 × step rate** if it changed, and starts the timer.
- On each timer overflow, `timer_callback()` toggles the STEP pin. Every falling edge counts as one completed step; when `steps_remaining` reaches zero, motion stops and the timer is stopped.
- A timeout (expected move time + 1 s) prevents an endless wait if the interrupt ever fails to fire.
- Because pulse edges are generated by a hardware timer interrupt rather than software delays, timing is essentially jitter-free.
- **Synchronization is guaranteed by wiring:** both drivers receive the exact same electrical STEP and DIR signals, so there is no possibility of relative timing skew between the motors.

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

Pulses are timed with Python's `sleep()`, so Linux scheduling introduces jitter. This is acceptable for a single motor at low speed but is not suitable for precise or synchronized multi-motor motion — which is why the project moved to the Arduino Uno R4.

---

## Known limitations / TODO

- [ ] **Hardware: set SW5–8 to OFF ON ON ON (400 pulses/rev) on both drivers.** The old all-OFF setting is 25000 pulses/rev.
- [ ] **Hardware: set S2 to 5V on both drivers.** It ships at 24V, and all the control signals here are 5 V.
- [ ] **Current is below the motor rating.** SW1–3 at OFF OFF ON gives 2.37 A peak / 1.69 A RMS, while the motor is rated 3.0 A. Raise it if you need more torque.
- [ ] **Pi logic-level issue.** With PUL+/DIR+ tied to 5V and PUL-/DIR- driven by 3.3V GPIO, the input sees about 1.7 V when the pin is "high". The V4.0 spec needs 0–0.5 V for low and 4.5–5 V for high, so 1.7 V is in neither range and the driver may miss or add pulses. The signals are also active-low. Use NPN buffers as in the Arduino design.
- [ ] **No acceleration/deceleration ramps.** Both controllers start and stop at full speed, which can cause stalls or missed steps at higher speeds or loads.
- [ ] **Arduino: blocking move.** `moveSteps()` blocks the main loop, so no other work (serial commands, sensors) can run during a move.
- [ ] **No application logic yet.** The code only runs a back-and-forth test. Commanding positions for the variable stiffness mechanism still has to be written.
- [ ] **Repo cleanup.** Remove the ` (1)` suffix from the `.ino` filename (Arduino IDE also expects the sketch in a folder with the same name), and add Python entries (e.g. `__pycache__/`, `*.pyc`) to `.gitignore`.
- [x] ~~Arduino timer re-created on every move (hang after a few moves)~~: fixed; the timer is now set up once in `setup()`.
- [x] ~~Arduino ignored timer setup failures~~: fixed; setup failures now halt with an error message and a blinking LED, and moves time out.
- [x] ~~Duplicate `stepper_control (1).py`~~: removed.

---

## Team

MEEN 402 — Texas A&M University

_TODO: team members, advisor, semester._
