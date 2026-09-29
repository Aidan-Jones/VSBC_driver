# VSBC Tensile Tester

Control software for the tensile tester built for the **Variable Stiffness** capstone project (MEEN 402, Texas A&M University).

_TODO: one-paragraph description of the variable stiffness mechanism and what the tester measures._

Two Stepperonline 23HS30-3004S stepper motors, each on a DM542T V4.0 driver, turn two 5 mm-lead ball screws that move the crosshead. A load cell on an HX711 amplifier measures the force. The system has two computers:

- an **Arduino Mega 2560**, which does everything that needs exact timing: it steps both motors in lockstep from a hardware timer, reads the load cell, and stops the motors by itself on overload, specimen break or travel limits;
- a **Raspberry Pi 5**, which runs the Python app `vsbc`. The app shows load and displacement live, runs tests, calibrates the load cell and the motors, and saves data and logs.

The Mega firmware also works on its own from the Arduino IDE Serial Monitor, without the Pi.

```
Raspberry Pi 5 ── Python app "vsbc" ─────────────────────────────────────────
  GUI (touchscreen)      command line (terminal, test, calibrate, flash)
       │                         │
  pull-to-failure test    calibration: load cell, travel, direction, limits
       │
  Machine API ── serial link ── (or the built-in simulator, for a PC without hardware)
       │
  data files, plots, logs  →  Outputs/
─────────────────── USB serial, 115200 baud, line protocol ───────────────────
Arduino Mega 2560 ── firmware/vsbc_firmware ─────────────────────────────────
  Timer1 step engine (both motors in lockstep)      HX711 load-cell reader
  settings in EEPROM (calibration, limits)          telemetry: one data line per load sample
  safety stops: overload, specimen break, travel limits, silent host (watchdog)
```

---

## Contents

- [Repository layout](#repository-layout)
- [Hardware and wiring](#hardware-and-wiring)
- [Quick start](#quick-start)
- [Using the GUI](#using-the-gui)
- [Calibration](#calibration)
- [Running a pull-to-failure test](#running-a-pull-to-failure-test)
- [Output files](#output-files)
- [Command line](#command-line)
- [Arduino IDE / Serial Monitor only](#arduino-ide--serial-monitor-only)
- [Configuration](#configuration)
- [Safety and limitations](#safety-and-limitations)
- [Hardware bring-up checklist](#hardware-bring-up-checklist)
- [Development](#development)
- [Known limitations / TODO](#known-limitations--todo)
- [References](#references)

---

## Repository layout

| Path | What it is |
|---|---|
| [`firmware/vsbc_firmware/`](firmware/vsbc_firmware/) | Arduino Mega sketch. Open `vsbc_firmware.ino` in the Arduino IDE, or build it with `vsbc flash`. No extra libraries. |
| [`vsbc/`](vsbc/) | Python package that runs on the Pi (and on a PC) |
| [`vsbc/gui/`](vsbc/gui/) | PyQt5 + pyqtgraph touchscreen GUI |
| [`vsbc/sim.py`](vsbc/sim.py) | Simulated Mega, so the app and tests run without hardware |
| [`tests/`](tests/) | pytest suite (runs against the simulator) |
| [`docs/protocol.md`](docs/protocol.md) | The serial protocol: every command, reply, event and data line |
| [`docs/hardware.md`](docs/hardware.md) | Drivers (DIP switches, S2), wiring details, speed limit, step engine |
| [`scripts/install_pi.sh`](scripts/install_pi.sh) | One-time Raspberry Pi setup |
| [`config.example.toml`](config.example.toml) | Optional settings for the app; copy to `config.toml` |
| `Outputs/` | Test data, calibration records and logs (not in git) |

The earlier code (`dual_stepper_mega`, `single_driver_test`, `serial_logger.py`, the Pi GPIO prototype `Controller1.0.py`) was replaced by this design. It is still in the git history, e.g. commit `cccd861`.

---

## Hardware and wiring

| Component | Part | Notes |
|---|---|---|
| Stepper motor (×2) | Stepperonline 23HS30-3004S | NEMA 23, 1.8°/step, 3.0 A/phase |
| Ball screw (×2) | 5 mm lead, direct coupled | 0.0125 mm per step at 400 pulses/rev |
| Stepper driver (×2) | Stepperonline DM542T **V4.0** | S2 = **5V**, DIP = OFF OFF ON ON **OFF ON ON ON** on both. See [docs/hardware.md](docs/hardware.md). |
| Controller | Arduino Mega 2560 | Real-time stepping, load cell, safety stops |
| Host | Raspberry Pi 5 (Raspberry Pi OS Bookworm, 64-bit, desktop) | Runs `vsbc`; connects to the Mega over USB |
| Load cell amplifier | HX711 | Any 24-bit HX711 board. Another ADC can be added later (see `firmware/vsbc_firmware/loadcell.h`). |
| Load cell | _TODO: model and capacity_ | Set `capacity_n` in `config.toml` |
| Motor power supply | _TODO: voltage / current rating_ | 18–50 VDC for the DM542T |

### Wiring

Motors (same as before; details and diagrams in [docs/hardware.md](docs/hardware.md)):

| Mega pin | Signal | Goes to |
|---|---|---|
| D22 | STEP A | Driver A PUL+ |
| D23 | STEP B | Driver B PUL+ |
| D24 | DIR A | Driver A DIR+ |
| D25 | DIR B | Driver B DIR+ |
| GND | — | PUL− and DIR− of both drivers |

ENA+/ENA− stay unconnected (drivers enabled).

Load cell:

| HX711 pin | Connects to |
|---|---|
| VCC | Mega 5V |
| GND | Mega GND |
| DT (DOUT) | Mega **D26** |
| SCK | Mega **D27** |
| E+ / E− | Load cell excitation + / − (usually red / black) |
| A+ / A− | Load cell signal + / − (usually green / white) |

- The pins are set in [`config.h`](firmware/vsbc_firmware/config.h) (`HX711_DOUT_PIN`, `HX711_SCK_PIN`).
- Most HX711 boards sample at **10 per second**. Tying the RATE pin to VCC (a jumper on some boards) gives **80 per second**, which makes break detection react 8× faster. The firmware measures the rate and reports it.
- If a force in the tension direction reads negative, that is fine: calibration takes care of the sign.

The Pi connects to the Mega with a USB cable. Opening the port resets the Mega, so the position has to be set again after the app connects (see [Position reference](#position-reference)).

---

## Quick start

### Raspberry Pi (the self-contained system)

```bash
git clone https://github.com/Aidan-Jones/VSBC_driver.git
cd VSBC_driver
bash scripts/install_pi.sh     # apt packages, Python environment, arduino-cli, desktop launcher
# log out and back in once (serial-port permission)
vsbc flash                     # compile + upload the firmware to the Mega
vsbc                           # start the GUI (also in the desktop menu as "VSBC Tensile Tester")
```

`vsbc gui --fullscreen` fills the screen for a kiosk-style touchscreen. The install script asks whether to start it automatically at login.

### A Windows / macOS / Linux PC, with or without hardware

Python 3.11 or newer.

```powershell
# Windows. Keep the environment outside OneDrive so thousands of files don't get synced.
py -3.12 -m venv $HOME\.venvs\vsbc
& $HOME\.venvs\vsbc\Scripts\Activate.ps1
pip install -e ".[gui,dev]"
vsbc gui --sim                 # the whole app, running against the simulator
```

On macOS / Linux: `python3 -m venv ~/.venvs/vsbc && source ~/.venvs/vsbc/bin/activate`, then the same `pip` and `vsbc` commands.

The simulator behaves like the real firmware: it has a noisy load cell, safety stops, and a specimen that yields and breaks. With a Mega plugged in, leave out `--sim`.

### Arduino IDE only

Open `firmware/vsbc_firmware/vsbc_firmware.ino`, select **Arduino Mega or Mega 2560**, upload, and open the Serial Monitor at **115200 baud**. See [Arduino IDE / Serial Monitor only](#arduino-ide--serial-monitor-only).

---

## Using the GUI

```
┌───────────────────────────────────────────────────────────────────────────┐
│ [port ▼] [⟳] [Disconnect]   /dev/ttyACM0 · vsbc_firmware 2.0.0     [STOP] │
│  LOAD          POSITION          EXTENSION          STATE                 │
│  123.45 N      12.3450 mm        2.3450 mm          TEST                  │
├──────────────────────────────────────────┬────────────────────────────────┤
│ [Load – extension] [Load & position – time] │ [Test] [Jog] [Calibrate]    │
│                                          │ [Console] [Firmware]           │
│        (live plot)                       │  (tab contents)                │
└──────────────────────────────────────────┴────────────────────────────────┘
```

- **STOP** (or **Esc** / **Space**) stops the motors immediately. It is always enabled. Any command sent while the motors are moving also stops them.
- **Connect**: pick the Mega's port (or "Auto-detect", or "Simulator") and press Connect. Warnings (load cell not found, not calibrated, position not referenced) appear in a banner under the readouts.
- **Readouts**: load (N), crosshead position (mm), extension since the test started (mm), and machine state.
- **Plots**: load vs extension for the current test, and load and position vs time (the last 60 s).

Tabs:

| Tab | What it does |
|---|---|
| **Test** | Specimen ID, rate (mm/min), maximum extension, load and break limits, optional area and gauge length, tare before starting, return afterwards. **Start test**, then the results summary and **Open results folder**. |
| **Jog** | Move the crosshead: rate, step (0.01 / 0.1 / 1 / 10 mm), ▲ / ▼ buttons (also the arrow keys), Go to, Home, Tare. Set the position reference. Service mode: move one motor only. |
| **Calibrate** | Shows the calibration stored on the Mega. Load-cell and travel wizards, direction check, travel limits, overload cutoff. |
| **Console** | Raw serial terminal (like the Serial Monitor) and a log of events. |
| **Firmware** | Shows the firmware version; compiles and uploads the firmware (arduino-cli). |

### Position reference

The motors are open loop: there is no encoder or limit switch, so the Mega only knows the position relative to where it was told "this is zero". Opening the USB port resets the Mega, and a reset loses the position.

After connecting, jog to your reference position (for example the grips at a marked gap), then **Jog → Set current position to 0**. The travel limits are measured from this reference, and the Mega refuses to start a test until the position is referenced. When the app closes cleanly it saves the position. **Restore last saved position** puts it back, but only use it if the crosshead has not moved since.

---

## Calibration

Everything is stored in the Mega's EEPROM, so the Mega is the source of truth. The same calibration works from the Pi and from the Arduino IDE. Each calibration done in the app also writes a timestamped record to `Outputs/calibration/`, and every test's `meta.json` includes the calibration in use.

Do them in this order the first time:

1. **Direction check** (Calibrate tab). The crosshead moves +1 mm; confirm it moved in the tension direction (grips apart). If not, the app flips `dir_invert` and saves it. You can also check motor B on its own; do this only with the screws uncoupled.
2. **Travel (steps/mm)**. The crosshead moves a set distance (default 20 mm). You measure the real travel with calipers or a dial indicator and enter it; repeat a few times if you like. The app sets `steps_per_mm = old × commanded ÷ measured`. An error of more than 5 % is flagged as a problem to fix, not calibrate away: most likely the microstep DIP switches or the screw lead. For example, a 0.016 ratio means the drivers are still at 25000 pulses/rev. Afterwards, set the position reference again.
3. **Load cell**. With nothing on the load cell, **Tare + record zero**. Then apply known loads in the tension direction, for example by hanging weights. Enter each one in g, kg, lb or N and press **Record**; each point is averaged over 3 s. Use 2–5 loads that span the range you will test in. The app fits a straight line (least squares) and shows R² and the worst error in N and % of capacity. **Save** writes `load_scale` and `load_offset` to the Mega and offers to set the overload cutoff to 90 % of `capacity_n`.
4. **Travel limits**. Jog to the lowest and highest safe positions and press **Set lower / upper travel limit here**. Any move beyond them is refused.
5. **Overload cutoff** (`max_load`). Any move stops when |load| reaches it while the load is rising. Set it below the load cell's and the frame's rating.

The command line does the same with `vsbc calibrate load` and `vsbc calibrate travel`. In the Serial Monitor, a single-point load calibration is `TARE`, hang a known weight, `CAL <newtons>`, then `SAVE`.

---

## Running a pull-to-failure test

1. Connect, set the position reference, and mount the specimen. Jog to take up slack if needed.
2. In the **Test** tab, set:
   - **Specimen ID**: used for the results folder name.
   - **Rate** in mm/min, from 0.12 to 600. ASTM D638, for example, uses 5 or 50 mm/min.
   - **Max extension**: the test stops here at the latest. Start position + max extension must be inside the travel limits.
   - **Stop at load** (optional).
   - **Break: drop from peak**: the load must fall this far below its peak (default 40 %) on two samples in a row. **Break: min peak** arms the detection only after the peak reaches this load, which ignores noise at the start.
   - **Area** and **gauge length** (optional): add stress and strain to the data and the results.
3. Press **Start test**. The app checks the setup first and lists anything that stops the test from starting. It then tares (if selected), sends the per-test limits to the Mega, and starts a constant-rate test move.
4. The test ends when the Mega detects the break, a load limit, the max extension, STOP, or a problem (load cell lost, host silent). The Mega makes the stop decision itself, so a frozen Pi cannot let the test run on.
5. The results appear in the Test tab and are saved in a new folder.

Stiffness is fitted between 10 % and 40 % of the peak load. The displacement comes from the motor steps (crosshead), so it includes the stretch of the frame and grips: stiffness and modulus are **apparent** values.

---

## Output files

Everything goes into `Outputs/` (change it with `output_dir` in `config.toml` or `--output-dir`):

```
Outputs/
├── tests/2026-09-29/153012_A1/
│   ├── data.csv        every sample, written while the test runs
│   ├── meta.json       test settings, firmware, machine settings, calibration, start/end, stop reason
│   ├── summary.json    results (summary.txt: the same, readable)
│   ├── summary.txt
│   ├── plot.png        load vs extension (+ stress vs strain with area and gauge length)
│   └── serial.log      serial traffic during the test (without data lines)
├── calibration/2026-09-29_150100_load.json   one record per calibration
├── logs/vsbc.log                             app log (rotating)
├── logs/2026-09-29_15-00-02_serial.log       each connection's serial session
└── last_position.json                        position saved at the last clean exit
```

`data.csv` columns:

| Column | Meaning |
|---|---|
| `time_s` | Time since the test started, from the Mega's clock (no USB jitter) |
| `position_mm` | Crosshead position relative to the reference |
| `extension_mm` | Position − position at the start of the test |
| `load_N` | Load |
| `stress_MPa`, `strain` | Only if area / gauge length were given (strain from the crosshead) |
| `raw_counts` | Raw HX711 reading |
| `state` | `R` during the test |

The results include peak load and the extension at peak, break load and extension, apparent stiffness (with R²), and energy to peak and to break. With area and gauge length they also include UTS, apparent modulus, strain at peak and elongation at break. `vsbc analyze <folder>` recalculates them, for example with a different stiffness window: `--fit 0.2 0.5`.

---

## Command line

Every command that talks to the machine accepts `--port` (default: auto-detect) and `--sim` (use the simulator).

| Command | What it does |
|---|---|
| `vsbc` or `vsbc gui [--fullscreen]` | Start the GUI |
| `vsbc terminal` | Serial terminal with a session log (replaces `serial_logger.py`). Sends STOP on exit. |
| `vsbc test --specimen A1 --rate 5 --max-ext 20 [--area 12.5 --gauge 50]` | Pull-to-failure test without the GUI (e.g. over SSH), with a live text readout. Ctrl+C stops it. |
| `vsbc calibrate load` / `vsbc calibrate travel` | Calibration with text prompts |
| `vsbc analyze <test folder>` | Recalculate a test's summary and plot |
| `vsbc flash [--port ...] [--compile-only]` | Compile and upload the firmware with arduino-cli |
| `vsbc ports` | List serial ports |

`vsbc --help` and `vsbc <command> --help` list all options. `python -m vsbc ...` works too.

---

## Arduino IDE / Serial Monitor only

The firmware accepts typed commands, so the machine can be used without the Pi. Open the Serial Monitor at **115200 baud**; any line ending works. Commands are case-insensitive, and one-letter aliases can be followed directly by a number (`v0.5`, `m-5`).

| Command | Alias | Action |
|---|---|---|
| `?` | `HELP` | Command list and the current state |
| `ZERO [mm]` | `Z` | Make the current position the reference (default 0) |
| `RATE <mm/s>` | `V` | Rate for all moves, 0.002–10 mm/s (5 mm/min = 0.0833) |
| `MOVE <±mm>` | `M` | Relative move, + = tension |
| `GOTO <mm>` / `HOME` | `G` / `H` | Absolute move / go to 0 |
| `RUN <±mm>` | `R` | Test move: like MOVE, but break detection and the stop load are active |
| `STOP` | `X` | Stop. **Any other command also stops a running move.** |
| `TARE` | `T` | Zero the load cell (no load applied) |
| `CAL <N>` | | Span calibration with a known load in newtons |
| `SET <key> <value>` | | Change a setting, e.g. `SET max_load 450`, `SET break_drop 40`, `SET break_min 5` |
| `GET` / `STATUS` | / `S` | All settings / the current state |
| `SAVE` | | Store calibration and limits in EEPROM |
| `STREAM ON` | | A data line for every load sample (default: every 0.5 s while moving) |
| `MOTORS A` / `B` / `AB` | | Service mode: pulse one driver only (bring-up) |

Data lines look like `D 123456 12.3450 87.512 1419842 R`: Mega time in ms, position (mm), load (N), raw counts, state. Moves end with a line such as `EVT MOVE_END reason=BREAK ... peak=401.220`. [docs/protocol.md](docs/protocol.md) has every command, reply, error code and setting.

---

## Configuration

**App settings** (`config.toml`, optional): copy [`config.example.toml`](config.example.toml) to `config.toml` in the repository folder, or to `~/.config/vsbc/config.toml`. It sets:
- the serial port and baud rate;
- the output folder;
- the load-cell capacity and local gravity;
- default test settings;
- the watchdog timeout;
- GUI refresh rate, font size and full screen.

**Machine calibration** (steps/mm, load scale, travel limits, overload cutoff, directions): stored on the Mega and changed from the Calibrate tab or with `SET` + `SAVE`. It is not in the config file.

**Firmware constants** ([`config.h`](firmware/vsbc_firmware/config.h)):
- pins;
- `STEPS_PER_REV` (must match SW5–SW8) and `SCREW_LEAD_MM`;
- `MAX_MOTOR_RPM` (120), which caps the rate at 10 mm/s;
- the HX711 pins and gain;
- optional limit-switch and E-stop pins (−1 = not fitted).

Change them, then upload again.

---

## Safety and limitations

- **Fit a hardware E-stop that cuts motor power.** The firmware's stops are software; a stuck-on driver or a firmware fault is only stopped by cutting power.
- **Open loop.** The Mega counts steps; nothing measures the actual position. A stall (motor slipping steps) is not detected. Signs are an elapsed time that doesn't match distance ÷ rate, buzzing or grinding, or a short measured displacement. If you see any of them, lower the rate or raise the driver current.
- **No limit switches** yet. Software travel limits (`min_pos`/`max_pos`) protect the screws only if the position reference is right. The firmware already supports switch inputs (`LIMIT_MIN_PIN`, `LIMIT_MAX_PIN`, `ESTOP_PIN` in `config.h`); wire them to GND when triggered.
- The Mega stops a move on its own when:
  - **overload**: |load| ≥ `max_load` while the load is rising, or the HX711 saturates;
  - **test limits**: during a test, the stop load or the break condition is reached;
  - **load cell lost**: during a test, the load cell stops responding for 0.5 s;
  - **watchdog**: the app has sent nothing for 3 s, e.g. the Pi crashed or the cable was unplugged.
- **No acceleration ramp.** Moves start at full rate on purpose, to keep the displacement rate constant from the first step. The 120 RPM cap keeps starts reliable; [docs/hardware.md](docs/hardware.md) explains why.
- **Service mode** (one motor only) racks the crosshead if the screws are coupled. Use it for bring-up only.

---

## Hardware bring-up checklist

1. **Drivers**: S2 = 5V and DIP = OFF OFF ON ON OFF ON ON ON on **both** drivers. Motor connected before power-up.
2. **Firmware**: upload from the Arduino IDE or `vsbc flash`. In the Serial Monitor, `?` should print the menu; `STATUS` should show `lc=1` if the HX711 is wired.
3. **One motor at a time**, screws uncoupled if possible: `MOTORS A`, `Z`, `V1`, `M5`. Expect 400 steps, 5.0 mm of screw travel in 5.0 s. Then `MOTORS B` and repeat.
4. **Both motors**: `MOTORS AB`, `M5`. Both screws move together, the same distance and direction. If B turns the wrong way, `SET invert_b 1`, `SAVE`.
5. **Cancel**: `V0.5`, `M20`, then type `x` + Enter mid-move. It stops at once, and `H` returns to 0.
6. **App**: on the Pi, run `vsbc`, connect, then do the calibration steps in [Calibration](#calibration) in order.
7. **Safety tests**, with a low `max_load`:
   - press on the load cell during a jog: `OVERLOAD` stop;
   - unplug the HX711 during a `RUN`: `LOADCELL` stop;
   - kill the app during a move: `WATCHDOG` stop within 3 s.
8. **First test**: a sacrificial specimen at a slow rate. Check that the elapsed time matches distance ÷ rate.

---

## Development

```bash
pip install -e ".[gui,dev]"
pytest                          # 56 tests against the simulator (incl. an offscreen GUI run), ~15 s
vsbc gui --sim                  # try GUI changes without hardware
vsbc flash --compile-only       # compile the firmware (needs arduino-cli)
```

- The protocol is defined in [docs/protocol.md](docs/protocol.md). The firmware (`commands.cpp`, `tester.cpp`, `motion.cpp`) and the simulator (`vsbc/sim.py`) both implement it. **Change all three together**; the simulator is what the tests run against.
- Both the firmware and the Python package carry a version number. The protocol version must match exactly (the app refuses to connect otherwise); a firmware version mismatch only gives a warning.
- The Mega sketch is split into modules; the Arduino IDE shows each file as a tab:
  - `motion.*`: Timer1 step engine;
  - `loadcell_hx711.cpp`: HX711;
  - `tester.*`: safety, averaging, telemetry;
  - `commands.*`: parser;
  - `settings.*`: EEPROM.
- The whole firmware uses about 23 kB of flash and 1.2 kB of RAM on the Mega 2560.

---

## Known limitations / TODO

- [ ] **Hardware: set SW5–8 to OFF ON ON ON (400 pulses/rev) on both drivers.** The old all-OFF setting is 25000 pulses/rev; the travel calibration flags it.
- [ ] **Hardware: set S2 to 5V on both drivers.** It ships at 24V.
- [ ] **Choose and wire the load cell + HX711** (DT → D26, SCK → D27). If possible set the HX711 to 80 samples/s. Put its capacity in `config.toml`.
- [ ] **Run the new firmware on the real Mega.** It compiles cleanly (arduino-cli, `arduino:avr:mega`) and the app is tested against the simulator, but neither has run on the hardware yet.
- [ ] **Try the GUI on the Pi 5** (tested on Windows).
- [ ] **Set real travel limits and the overload cutoff** after the first calibration.
- [ ] **Fit a hardware E-stop** that cuts motor power; optionally limit switches (pins in `config.h`).
- [ ] **Current is below the motor rating.** SW1–3 at OFF OFF ON gives 2.37 A peak vs the motor's 3.0 A. Raise it if you need more torque.
- [ ] Extension is measured at the crosshead, so it includes frame and grip compliance. A compliance correction or an extensometer would give true specimen strain.
- [ ] Only pull-to-failure is built in. Cyclic, hold/relaxation and compression tests can follow the same pattern (`RUN` with a negative distance already moves in compression).

---

## References

Similar open-source testers looked at for this design:

- [CNCKitchen/Open-Pull](https://github.com/CNCKitchen/Open-Pull): Arduino + HX711 + two steppers, controlled from a serial terminal. It shows why the load cell and motion both belong on the microcontroller.
- [FrancescoNegri/universal-testing-machine](https://github.com/FrancescoNegri/universal-testing-machine): a Raspberry Pi-only tester. The idea for timestamped JSON calibration records comes from here. It reads the HX711 from Linux, which this design avoids (see below).
- Raspberry Pi forum threads on HX711 bit-banging from Linux ([random values](https://forums.raspberrypi.com/viewtopic.php?t=322131), [hanging](https://forums.raspberrypi.com/viewtopic.php?t=370007)) report occasional bad readings, and note that RPi.GPIO does not run on a Pi 5. This is why the HX711 is read by the Mega.
- [MrYsLab/telemetrix](https://github.com/MrYsLab/telemetrix): a Firmata-style "Python drives the Arduino" library. Its steppers are timed from `loop()`, which is not precise enough for constant-rate lockstep, so this design keeps its own Timer1 engine and protocol.
- [Real-Time-Py-Serial-Plotter](https://github.com/iskandarputra/Real-Time-Py-Serial-Plotter): the PyQt5 + pyqtgraph pattern used for the live plots.
- [Arduino CLI on a Raspberry Pi](https://siytek.com/arduino-cli-raspberry-pi/): how `vsbc flash` compiles and uploads.
- [Instron: tensile testing guide](https://www.instron.com/en/resources/test-types/tensile-test/): end-of-test by % load drop from peak; size the load cell so peak loads are 10–90 % of its capacity.

---

## Team

MEEN 402 — Texas A&M University

_TODO: team members, advisor, semester._
