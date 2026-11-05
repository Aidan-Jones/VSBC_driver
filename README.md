# VSBC Tensile Tester

Control software for the tensile tester built for the **Variable Stiffness** capstone project (MEEN 402, Texas A&M University).

_TODO: one-paragraph description of the variable stiffness mechanism and what the tester measures._

Two Stepperonline 23HS30-3004S stepper motors, each on a DM542T V4.0 driver, turn two 5 mm-lead ball screws that move the crosshead. A load cell on a SparkFun HX711 amplifier measures the force. The system has two computers:

- an **Arduino Mega 2560**, which does everything that needs exact timing: it steps both motors in lockstep from a hardware timer, and stops the motors by itself on overload, specimen break or travel limits;
- a **Raspberry Pi 5**, which runs the Python app `vsbc`. It shows load and displacement live, runs tests, calibrates the load cell and the motors, and saves data and logs.

By default the load-cell amplifier is wired to the Mega, which reads it. The Mega firmware then also works on its own from the Arduino IDE Serial Monitor, without the Pi. The amplifier can be wired to the Pi's GPIO pins instead (`source = "pi"` in `config.toml`); the app then reads it and passes every reading to the Mega.

A Basler a2A1920-160umBAS camera records the specimen for **DIC** (digital image correlation, with pyvale). The camera scripts in [`camera/`](camera/) run inside the app (the **Camera** tab), with every test. Each data.csv row gets its own image, and the frames around the break are saved at the camera's full rate.

```
Raspberry Pi 5 ── Python app "vsbc" ─────────────────────────────────────────
  GUI (touchscreen)      command line (terminal, test, calibrate, flash, camera)
       │                         │
  pull-to-failure test    calibration: load cell, travel, direction, limits
       │
  Machine API ── serial link ── (or the built-in simulator, for a PC without hardware)
       │              ▲
       │              └── HX711 reader (GPIO5 / GPIO6, if source = "pi"): every reading goes to the Mega
       │
  camera process (.venv-camera) ── Basler camera over USB 3, camera/*.py scripts, DIC jobs after the test
       │
  data files, video, DIC results, plots, logs  →  Outputs/
─────────────────── USB serial, 115200 baud, line protocol ───────────────────
Arduino Mega 2560 ── firmware/vsbc_firmware ─────────────────────────────────
  Timer1 step engine (both motors in lockstep)      its own HX711 (or load readings from the Pi)
  settings in EEPROM (calibration, limits)          telemetry: one data line per load sample
  safety stops: overload, specimen break, load cell lost, travel limits, silent host (watchdog)
```

---

## Contents

- [Repository layout](#repository-layout)
- [Hardware and wiring](#hardware-and-wiring)
- [Quick start](#quick-start)
- [Using the GUI](#using-the-gui)
- [Calibration](#calibration)
- [Running a pull-to-failure test](#running-a-pull-to-failure-test)
- [DIC camera](#dic-camera)
- [Output files](#output-files)
- [Storage and copying outputs](#storage-and-copying-outputs)
- [Command line](#command-line)
- [Arduino IDE / Serial Monitor only](#arduino-ide--serial-monitor-only)
- [Legacy sketches (motion only)](#legacy-sketches-motion-only)
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
| [`firmware/legacy/`](firmware/legacy/) | The two old standalone sketches (motion only, no load cell), kept for reference. See [Legacy sketches](#legacy-sketches-motion-only). |
| [`vsbc/`](vsbc/) | Python package that runs on the Pi (and on a PC) |
| [`vsbc/gui/`](vsbc/gui/) | PyQt5 + pyqtgraph touchscreen GUI |
| [`camera/`](camera/) | The DIC camera scripts (capture, pyvale DIC, strain, crack tips). **Swappable**: drop updated versions in; see [camera/README.md](camera/README.md) |
| [`vsbc/camera/`](vsbc/camera/) | The app's side of the camera: the camera process, clock sync, post-processing jobs, simulated camera |
| [`vsbc/loadcell_pi.py`](vsbc/loadcell_pi.py) | Reads the HX711 on the Pi's GPIO pins |
| [`vsbc/sim.py`](vsbc/sim.py) | Simulated Mega, so the app and tests run without hardware |
| [`tests/`](tests/) | pytest suite (runs against the simulator) |
| [`docs/protocol.md`](docs/protocol.md) | The serial protocol: every command, reply, event and data line |
| [`docs/hardware.md`](docs/hardware.md) | Drivers (DIP switches, S2), wiring details, speed limit, step engine |
| [`scripts/install_pi.sh`](scripts/install_pi.sh) | One-time Raspberry Pi setup |
| [`config.example.toml`](config.example.toml) | Optional settings for the app; copy to `config.toml` |
| `Outputs/` | Test data, calibration records and logs (not in git) |

The two earlier standalone sketches (`dual_stepper_mega`, `single_driver_test`) are kept in [`firmware/legacy/`](firmware/legacy/). The PC logger `serial_logger.py` and the Pi GPIO prototype `Controller1.0.py` were replaced by this design; they are in the git history (commit `cccd861`).

---

## Hardware and wiring

| Component | Part | Notes |
|---|---|---|
| Stepper motor (×2) | Stepperonline 23HS30-3004S | NEMA 23, 1.8°/step, 3.0 A/phase |
| Ball screw (×2) | 5 mm lead, direct coupled | 0.0015625 mm per step at 3200 pulses/rev |
| Stepper driver (×2) | Stepperonline DM542T **V4.0** | S2 = **5V**, DIP = ON OFF OFF ON **ON ON OFF ON** on both (3.76 A peak, 3200 pulses/rev). See [docs/hardware.md](docs/hardware.md). |
| Controller | Arduino Mega 2560 | Real-time stepping, safety stops |
| Host | Raspberry Pi 5 (Raspberry Pi OS Bookworm or newer, 64-bit, desktop) | Runs `vsbc`; connects to the Mega over USB; reads the load-cell amplifier with `source = "pi"` |
| Load cell amplifier | SparkFun HX711 breakout (SEN-13879) | Wired to the Mega (default) or to the Pi's GPIO pins |
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

Load cell, **wired to the Raspberry Pi (optional)**:

| SparkFun HX711 pin | Connects to |
|---|---|
| VDD | Pi **3.3V** (header pin 17) |
| VCC | Pi **5V** (header pin 2) |
| GND | Pi GND (header pin 30) |
| DAT | Pi **GPIO5** (header pin 29) |
| CLK | Pi **GPIO6** (header pin 31) |
| E+ / E− (RED / BLK) | Load cell excitation + / − (usually red / black) |
| A+ / A− (GRN / WHT) | Load cell signal + / − (usually green / white) |

- **VDD must go to 3.3V, not 5V.** VDD sets the voltage on DAT, and the Pi's GPIO pins are damaged by 5 V.
- Select it with `source = "pi"` under `[loadcell]` in `config.toml`, or for one run with `--loadcell pi` (for example `vsbc gui --loadcell pi`).
- The pins are set under `[loadcell]` in `config.toml` (`dout_pin`, `sck_pin`, as GPIO numbers).
- With `source = "pi"`, `vsbc loadcell` reads the amplifier for 5 s and prints the raw counts, the sample rate and the share of readings it discarded. Run it before anything else; it does not need the Mega.
- If the Pi gets no readings at all when the app connects, the app switches to the HX711 wired to the Mega and says so in a warning.
- The Pi only reads the amplifier. It sends every reading to the Mega, which still does the tare, the calibration and the safety stops. [docs/hardware.md](docs/hardware.md) explains the timing check that discards a reading.

Load cell, **wired to the Mega (default)**:

| HX711 pin | Connects to |
|---|---|
| VCC and VDD | Mega 5V |
| GND | Mega GND |
| DAT (DOUT) | Mega **D26** |
| CLK (SCK) | Mega **D27** |
| E+ / E−, A+ / A− | Load cell, as above |

- This is the default (`source = "mega"` under `[loadcell]` in `config.toml`, or for one run `--loadcell mega`).
- The pins are set in [`config.h`](firmware/vsbc_firmware/config.h) (`HX711_DOUT_PIN`, `HX711_SCK_PIN`).
- `vsbc loadcell` connects to the Mega and prints the raw counts for 5 s. If none arrive, it says which wire to check: no conversions at all (DAT stays high: power, GND or DAT) or readings the HX711 did not answer (DAT stays low: CLK or VDD). `?` in the Serial Monitor prints the same reason.
- This is the wiring to use without the Pi (Arduino IDE Serial Monitor only), and on a PC: there the app cannot reach GPIO pins, so with `source = "pi"` it says so in a warning and uses the Mega's HX711.

Both wirings:

- The HX711 samples at **10 per second** as shipped, and the app expects 10 (`rate_hz` under `[loadcell]` in `config.toml`). It compares that with the rate the firmware measures and warns at connect when they differ. Cutting the RATE jumper on the back of the SparkFun board gives **80 per second** ([how](docs/hardware.md#hx711-sample-rate)): break detection then reacts 8× faster, and the camera gets 8× more frames paired with load readings. Set `rate_hz = 80` if you do.
- **Tension must read as positive load.**
  - The break detection and the results depend on it.
  - The calibration sets the sign. The load-cell wizard asks whether the known loads hang from the load cell (tension) or are placed on it (compression); on the command line, use `--compression`.
  - If tension still shows negative, **Calibrate → Flip load sign** fixes it without recalibrating. The app warns during a test when this happens.
- The calibration is stored on the Mega for both. Calibrate again after moving the amplifier from one to the other.

The Pi connects to the Mega with a USB cable. Opening the port resets the Mega, so the position has to be set again after the app connects (see [Position reference](#position-reference)).

---

## Quick start

### Raspberry Pi (the self-contained system)

Starting from a Pi with only Raspberry Pi OS installed and an internet connection:

1. In a terminal, download and install:

   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/Aidan-Jones/VSBC_driver.git
   cd VSBC_driver
   bash scripts/install_pi.sh     # apt packages, Python environment, arduino-cli, desktop launcher
   ```

2. **Log out and back in once** (or reboot). This gives your user permission to use the Mega's serial port and the GPIO pins.

3. Then:

   ```bash
   vsbc flash                     # compile + upload the firmware to the Mega
   vsbc loadcell                  # check the load cell (wired to the Mega)
   vsbc                           # start the GUI (also in the desktop menu as "VSBC Tensile Tester")
   ```

4. For the DIC camera, add its environment once (about 1 GB, 10–20 minutes):

   ```bash
   bash scripts/install_pi.sh --camera   # .venv-camera with pypylon + pyvale, Basler udev rule,
                                         # USB camera memory (reboot once), ffmpeg
   vsbc camera check                     # frame rate and dropped frames, disk speed, a small pyvale DIC
   ```

If the terminal says `vsbc: command not found`, the install script did not finish (it stops at the first error): run it again and read its last lines. The command itself is at `VSBC_driver/.venv/bin/vsbc` and can always be run by that full path, for example `~/VSBC_driver/.venv/bin/vsbc flash`.

Everything the Pi needs is in the repository or installed by the script; nothing has to be copied over by hand. `vsbc gui --fullscreen` fills the screen for a kiosk-style touchscreen. The install script asks whether to start it automatically at login.

To update a Pi that is already set up:

```bash
cd VSBC_driver
git pull
bash scripts/install_pi.sh     # safe to run again; picks up new dependencies
vsbc flash                     # only needed when the firmware changed (the app says so when it connects)
```

Your `config.toml`, the Python environment and `Outputs/` are not in git, so `git pull` leaves them alone.

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

The simulator behaves like the real firmware: it has a noisy load cell, safety stops, and a specimen that yields and breaks. With `--sim` the camera is simulated too: a speckle pattern that stretches with the extension and cracks at the break. With a Mega plugged in, leave out `--sim`.

To use the real camera on a PC, point `[camera] python` in `config.toml` at a Python environment with pypylon, pyvale and OpenCV (for example the laptop's conda `dic` environment). Keep `Outputs/` (`output_dir`) outside OneDrive: a recording writes gigabytes.

### Arduino IDE only

Open `firmware/vsbc_firmware/vsbc_firmware.ino`, select **Arduino Mega or Mega 2560**, upload, and open the Serial Monitor at **115200 baud**. See [Arduino IDE / Serial Monitor only](#arduino-ide--serial-monitor-only).

---

## Using the GUI

```
┌───────────────────────────────────────────────────────────────────────────┐
│ [port ▼] [⟳] [Disconnect]   /dev/ttyACM0 · vsbc_firmware 2.3.0     [STOP] │
│  LOAD          POSITION          EXTENSION          STATE                 │
│  123.45 N      12.3450 mm        2.3450 mm          TEST                  │
├──────────────────────────────────────────┬────────────────────────────────┤
│ [Camera] [Load – extension]              │ [Test] [Camera] [Outputs]      │
│ [Load & position – time] [Results]       │ [Jog] [Calibrate] [Units] ...  │
│   (camera preview, live plot, results)   │  (tab contents)                │
└──────────────────────────────────────────┴────────────────────────────────┘
```

- **STOP** (or **Esc** / **Space**) stops the motors immediately. It is always enabled. Any command sent while the motors are moving also stops them.
- **Connect**: pick the Mega's port (or "Auto-detect", or "Simulator") and press Connect. Warnings (load cell not found, not calibrated, position not referenced, storage low) appear in a banner under the readouts.
- **Readouts**: load, crosshead position, extension since the test started, and machine state, in the units chosen in the Units tab.
- **Left tabs**:
  - **Camera** (the default): the live preview, the ROI editor and the high-speed button (see [DIC camera](#dic-camera)).
  - **Load – extension**: the current test.
  - **Load & position – time**: the last 60 s. Until the first test after starting the app, only the last 20 s.
  - **Results**: a finished test's camera frames, with the frames where the crack happened picked out. Also its plots and summary (see [Viewing the results](#viewing-the-results)). Greyed out while a test or a camera recording runs.

Tabs on the right:

| Tab | What it does |
|---|---|
| **Test** | Specimen ID, rate, maximum extension, load and break limits, optional area and gauge length, tare before starting, return afterwards. **Start test**, the **⚡ High-speed camera** button during a test, then the results summary, **View results** (the Results tab) and **Open results folder**. |
| **Camera** | Camera settings from `camera/capture_dic_live.py`: recording with each test, frames per load sample, RAM buffer, settle time, load-cell delay, DIC options, and what runs after the test (video, DIC, strain, crack tips). It also checks the camera scripts and has Reload, Process a folder, and Check camera. |
| **Outputs** | Disk space, every test and recording with its size, **View results**, **Copy to a USB drive**, delete a test or only its camera images, delete old logs. |
| **Jog** | Move the crosshead: rate, step (0.01 / 0.1 / 1 / 10 mm, or 0.001–0.5 in), ▲ / ▼ buttons (also the arrow keys), Go to, Home, Tare. Set the position reference. Service mode: move one motor only. |
| **Calibrate** | Shows the calibration stored on the Mega. Load-cell and travel wizards, **flip load sign**, direction check, travel limits, overload cutoff. |
| **Units** | Force in N or lbf; travel in mm, cm, m or in. The GUI uses these units everywhere. data.csv keeps its N/mm columns and gets extra columns in the chosen units; summary.txt, plot.png and the video use them too. The choice is remembered. |
| **Console** | Raw serial terminal (like the Serial Monitor) and a log of events. |
| **Firmware** | Shows the firmware version; compiles and uploads the firmware (arduino-cli). |

The GUI sizes itself for the screen it opens on. Text, buttons, readouts and plot labels scale with the screen: 800 × 480 and 1024 × 600 touchscreens get 13 px text (the smallest size that is still easy to read), a 1280 × 800 screen gets `font_pt` (11 pt), and larger or high-resolution screens get proportionally bigger text. Sizes are in pixels, so a screen that reports a wrong physical size (common on HDMI touchscreens) does not make the text tiny or huge. On a narrow screen, button rows and the tab buttons wrap onto a second row instead of scrolling sideways. Only a tab taller than the screen scrolls, and only up and down. A window smaller than 1280 × 800 opens maximised. If the text is still too small or too large, set `scale` under `[gui]` in `config.toml`. It replaces the automatic factor: `scale = 1.2` gives 1.2 × `font_pt` on any screen. The log records the screen size and text size the GUI chose.

### Position reference

The motors are open loop: there is no encoder or limit switch, so the Mega only knows the position relative to where it was told "this is zero". Opening the USB port resets the Mega, and a reset loses the position.

After connecting, jog to your reference position (for example the grips at a marked gap), then **Jog → Set current position to 0**. The travel limits are measured from this reference, and the Mega refuses to start a test until the position is referenced. When the app closes cleanly it saves the position. **Restore last saved position** puts it back, but only use it if the crosshead has not moved since.

---

## Calibration

Everything is stored in the Mega's EEPROM, so the Mega is the source of truth. The same calibration works from the Pi and from the Arduino IDE. Each calibration done in the app also writes a timestamped record to `Outputs/calibration/`, and every test's `meta.json` includes the calibration in use.

Do them in this order the first time:

1. **Direction check** (Calibrate tab). The crosshead moves +1 mm; confirm it moved in the tension direction (grips apart). If not, the app flips `dir_invert` and saves it. You can also check motor B on its own; do this only with the screws uncoupled.
2. **Travel (steps/mm)**. The crosshead moves a set distance (default 20 mm). You measure the real travel with calipers or a dial indicator and enter it; repeat a few times if you like. The app sets `steps_per_mm = old × commanded ÷ measured`. An error of more than 5 % is flagged as a problem to fix, not calibrate away: most likely the microstep DIP switches or the screw lead. For example, a 0.016 ratio means the drivers are still at 25000 pulses/rev. Afterwards, set the position reference again.
3. **Load cell**. With nothing on the load cell, **Tare + record zero**.
   - Choose how the known loads act: hanging from or pulling on the load cell (tension), or placed on it (compression). Either way, tension comes out positive.
   - Apply each known load, enter it in g, kg, lb or N, and press **Record**. Each point is averaged over 3 s. Use 2–5 loads that span the range you will test in.
   - The app fits a straight line (least squares) and shows R² and the worst error, in your force unit and as % of capacity.
   - **Save** writes `load_scale` and `load_offset` to the Mega and offers to set the overload cutoff to 90 % of `capacity_n`.
   - If a later test shows tension as negative load, **Flip load sign** in the Calibrate tab fixes it.
4. **Travel limits**. Jog to the lowest and highest safe positions and press **Set lower / upper travel limit here**. Any move beyond them is refused.
5. **Overload cutoff** (`max_load`). Any move stops when |load| reaches it while the load is rising. Set it below the load cell's and the frame's rating.

The command line does the same with `vsbc calibrate load` (add `--compression` for weights placed on the load cell) and `vsbc calibrate travel`. In the Serial Monitor, a single-point load calibration is `TARE`, hang a known weight, `CAL <newtons>`, then `SAVE`.

### Load-cell calibration without the Mega

With the HX711 wired to the Pi, the load cell can be calibrated with no Arduino attached:

```bash
vsbc calibrate load --no-mega
```

It asks for the same zero point and known loads as step 3, reading the HX711 directly. The result is kept in `Outputs/pending_load_calibration.json`. The next time the app connects to the Mega (with the load cell read by the Pi), it stores the calibration on the Mega, deletes that file and says so in the warning banner. From then on the Mega holds the calibration, as usual.

- Command line only: the wizard in the Calibrate tab needs the Mega.
- The overload cutoff (`max_load`) and the travel calibration still need the Mega. Set the cutoff after the calibration has been stored.
- It is only valid for the HX711 on the Pi, at the gain set in `config.toml`.

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
3. Press **Start test**. The app checks the setup first and lists anything that stops the test from starting.
   - It then tares (if selected) and sends the per-test limits to the Mega.
   - With the camera on, it shows a hands-off countdown and takes the reference frame.
   - Then it starts a constant-rate test move.
4. The test ends when the Mega detects the break, a load limit, the max extension, STOP, or a problem (load cell lost, host silent). The Mega makes the stop decision itself, so a frozen Pi cannot let the test run on. The camera stops with the test, and if the camera fails, the test stops too.
5. The results appear in the Test tab and are saved in a new folder. With the camera, the video and the DIC follow in the background.

Stiffness is fitted between 10 % and 40 % of the peak load. The displacement comes from the motor steps (crosshead), so it includes the stretch of the frame and grips: stiffness and modulus are **apparent** values.

---

## DIC camera

The camera code runs in its own Python environment (`.venv-camera`, from `install_pi.sh --camera`), as a separate process. The camera settings (exposure, crop, buffer, DIC parameters) are the USER SETTINGS in [`camera/capture_dic_live.py`](camera/capture_dic_live.py). [camera/README.md](camera/README.md) has the details, and the interface the scripts must keep when you swap in new versions.

**Before the first test:**
- **Camera tab → Edit ROI**:
  - draw the region for DIC (include the notch);
  - place the seed well inside it;
  - place the notch tip;
  - set the crack direction.

  Use **1:1 zoom** to check the focus and **Exposure ±** to adjust (watch `sat` and `bright99.9`).
- **Matching the load cell and the camera.** The HX711 gives 10 load readings per second as shipped (80 with its RATE jumper cut). The camera runs at its full rate (about 478 fps with the 400-row crop) and keeps the last `BUFFER_SECONDS` in RAM.
  - **The rest of the test:** for every load reading it saves the frame taken nearest that moment (about ±1 ms), so every data.csv row has its own image. Load is interpolated to each frame's exact time.
  - **The important frames:** every frame of the RAM buffer is saved at the break (when the Mega detects it), and also while **⚡ High speed** is on. These frames carry the latest load reading, which is not interpolated across the break.
- **⚡ High speed** (Camera tab or Test tab) saves every frame from the RAM buffer onward, until you turn it off or the test ends. Press it when the break is near. The disk usually cannot keep up at the full rate, so frames wait in RAM; the camera line shows how long that can last before the oldest high-speed frames are dropped.
- **The test and the camera stop together.** When the test ends, the camera stops. If the camera stops (Stop recording, disk full, camera unplugged), the test stops too, and the results say why.
- **One clock.**
  - Frames get a `time_s` on data.csv's clock and zero: the Mega's clock, with zero at the first sample of the test.
  - `video.mp4` plays in real time from `time_s` = 0, so pausing it at 12.345 s shows the moment of data.csv's row at 12.345 s. Each frame shows the time and the load.
  - The load-cell delay setting compensates the HX711's filter lag. Measure it once with a tap test.
- **After the test**, at low priority, and paused while the next test records (Linux):
  1. the sync;
  2. the video;
  3. DIC;
  4. strain and plots;
  5. both crack trackers.

  Choose which ones in the Camera panel. **Process a folder…** or `vsbc camera process <folder>` runs them again.
- **Live DIC** during the test is off by default: on the Pi it competes with the camera for the CPU.
- A recording without a test: **● Record** in the Camera tab (it goes to `Outputs/camera/<date>/`).

### Viewing the results

The **Results** tab (left side) shows a finished test or camera recording. **View results** in the Test tab or the Outputs tab opens one there; the list at the top has them all, newest first. The tab is greyed out while a test or a camera recording runs, so reading frames never competes with the camera for the disk.

- **Frames**:
  - **Scrub and play.** Use the slider, ◀ / ▶ (one frame) and ◀◀ / ▶▶ (ten frames), or **Play** at 5, 15 or 30 fps.
  - **Overlays.** The ROI, the notch, and the crack tips from both trackers (red + image, cyan × opening, yellow ○ by hand) are drawn on the frame. **DIC map** shows pyvale's vertical displacement on the frames DIC ran on.
  - **Load–time curve.** The curve below the frame shows where the frame is. Click it to jump there. Full-rate windows are shaded orange.
- **The crack frames.** The Mega detects the break by comparing load readings, so the crack happened between the last reading before the load fell (the break load) and the next reading. Those times are moved back by the load-cell delay, onto the frames' clock.
  - The frames in between are the **crack interval**: about 48 at 10 load readings/s and 478 fps, about 6 at 80/s. They are shaded red on the curve and tagged **CRACK**.
  - **Crack frames only** plays the interval plus 50 frames either side, in a loop.
  - The **estimated crack frame** (★) is the frame that changes most from the one before, inside the ROI, in and just around the interval. **Jump to crack** goes there.
  - Without a detected break, the last full-rate window is shown instead.
  - If the frames around the break were not saved at full rate, the tab names the two frames the break fell between and says to turn on **Save the RAM buffer at the break**.
- **Plots**: plot.png and the scripts' plots, scaled to fit. **Open full size** opens one in the system viewer.
- **Summary**: summary.txt, the frame pairing and frame counts, the full-rate windows, the crack-tip trackers and any warnings.
- **Play video** opens video.mp4 in the system player. **⟳** reads the files again, for example when DIC finishes after you opened the test.

---

## Output files

Everything goes into `Outputs/` (change it with `output_dir` in `config.toml` or `--output-dir`):

```
Outputs/
├── tests/2026-09-29/153012_A1/
│   ├── data.csv        every sample, written while the test runs
│   ├── meta.json       test settings, firmware, machine settings, calibration, start/end, stop reason, units
│   ├── summary.json    results in N and mm (summary.txt: the same, readable, in the display units)
│   ├── summary.txt
│   ├── plot.png        load vs extension (+ stress vs strain with area and gauge length)
│   ├── serial.log      serial traffic during the test (without data lines)
│   ├── video.mp4       with the camera: playback time = data.csv time_s, time and load on each frame
│   └── camera/         with the camera: frames, frames.csv, DIC results, plots, crack tips (camera/README.md)
├── camera/2026-09-29/160000_noise/            recordings made without a test (Camera tab > Record)
├── camera/last_roi.npz                       the ROI, seed and notch tip from the Camera tab
├── calibration/2026-09-29_150100_load.json   one record per calibration
├── logs/vsbc.log                             app log (rotating)
├── logs/camera.log                           the camera process
├── logs/2026-09-29_15-00-02_serial.log       each connection's serial session
├── preferences.json                          the Units tab's choice
├── last_position.json                        position saved at the last clean exit
└── pending_load_calibration.json             only after `calibrate load --no-mega`, until the Mega has it
```

`data.csv` columns:

| Column | Meaning |
|---|---|
| `time_s` | Time since the test started, from the Mega's clock (no USB jitter), to 0.1 ms. The firmware stamps each reading when the HX711 signals it ready. |
| `position_mm` | Crosshead position relative to the reference |
| `extension_mm` | Position − position at the start of the test |
| `load_N` | Load (tension positive) |
| `stress_MPa`, `strain` | Only if area / gauge length were given (strain from the crosshead) |
| `raw_counts` | Raw HX711 reading |
| `state` | `R` during the test |
| `position_in`, `extension_in`, `load_lbf`, `stress_psi`, ... | Only with display units other than N and mm: the same values in those units |
| `frame`, `frame_dt_ms` | Only with the camera: the camera frame (`camera/images/frame_<frame>.tif`) saved for this row, and how far its time is from the row's (ms) |

The results include peak load and the extension at peak, break load and extension, apparent stiffness (with R²), and energy to peak and to break. With area and gauge length they also include UTS, apparent modulus, strain at peak and elongation at break. `vsbc analyze <folder>` recalculates them, for example with a different stiffness window: `--fit 0.2 0.5`.

---

## Storage and copying outputs

- **Storage warnings.**
  - A banner warns when the drive with `output_dir` has less than `warn_free_gb` free (10 GB), and turns red below `critical_free_gb` (2 GB). Both are set under `[storage]` in `config.toml`; the Outputs tab title gets a ⚠.
  - With the camera, the Camera panel estimates each test's size. Starting a test that would not fit is refused.
  - Below 1 GB free, the camera saves only the full-rate frames; below 0.3 GB it stops, and with it the test.
- **Outputs tab:**
  - disk usage and about how many more tests fit;
  - every test and recording with its size, camera images, video and DIC results;
  - **Copy to a USB drive**: plug it in, press ⟳, select tests, Copy, then **Eject**. Raw camera images are left out unless ticked. The copy goes to `VSBC_outputs/` on the drive and pauses while the camera records.
  - **Delete camera images** (frees most of the space; data.csv, video, DIC results and plots stay);
  - **Delete** a whole test;
  - delete logs older than 30 days.

---

## Command line

Every command that talks to the machine accepts `--port` (default: auto-detect), `--sim` (use the simulator) and `--loadcell pi|mega` (where the HX711 is wired; default from `config.toml`, normally `mega`).

| Command | What it does |
|---|---|
| `vsbc` or `vsbc gui [--fullscreen]` | Start the GUI |
| `vsbc terminal` | Serial terminal with a session log (replaces `serial_logger.py`). Sends STOP on exit. |
| `vsbc test --specimen A1 --rate 5 --max-ext 20 [--area 12.5 --gauge 50]` | Pull-to-failure test without the GUI (e.g. over SSH), with a live text readout. Ctrl+C stops it. |
| `vsbc calibrate load` / `vsbc calibrate travel` | Calibration with text prompts (`calibrate load --compression` for weights placed on the load cell) |
| `vsbc calibrate load --no-mega` | Load-cell calibration on the Pi alone; stored on the Mega at the next connection |
| `vsbc camera check [--sim]` | The camera's frame rate and dropped frames, the disk's write speed, the camera scripts, and a small synthetic pyvale DIC (catches pyvale version differences). Run it on the Pi before the first real test. |
| `vsbc camera process <folder> [--only video,dic,strain,crack_image,crack_opening]` | Sync, video, DIC, strain and crack tips for a test folder or a camera recording, without the GUI |
| `vsbc analyze <test folder>` | Recalculate a test's summary and plot |
| `vsbc flash [--port ...] [--compile-only]` | Compile and upload the firmware with arduino-cli |
| `vsbc ports` | List serial ports |
| `vsbc loadcell [--seconds 5]` | Check the load cell where `config.toml` says it is wired: raw counts and sample rate from the Mega (default), and why it is not responding if so; with `source = "pi"`, the HX711 on the Pi's GPIO pins (discarded readings too, no Mega needed). It first prints which config file it read. |

`vsbc --help` and `vsbc <command> --help` list all options. `python -m vsbc ...` works too. Command-line values are in mm and N; printed results use the display units (`[units]`, or the Units tab's last choice).

---

## Arduino IDE / Serial Monitor only

The firmware accepts typed commands, so the machine can be used without the Pi. For load readings in this mode the HX711 must be wired to the Mega (D26 / D27); with it wired to the Pi, only motion works. Open the Serial Monitor at **115200 baud**; any line ending works. Commands are case-insensitive, and one-letter aliases can be followed directly by a number (`v0.5`, `m-5`).

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

## Legacy sketches (motion only)

The two standalone sketches from before the redesign are kept in [`firmware/legacy/`](firmware/legacy/) for reference. They use the same wiring (D22–D25) and still upload and run from the Arduino IDE (Serial Monitor, 115200 baud).

| Sketch | What it does | Commands |
|---|---|---|
| [`dual_stepper_mega`](firmware/legacy/dual_stepper_mega/dual_stepper_mega.ino) | Both motors in lockstep: constant-rate displacement trials | `v<mm/s>` rate, `d<mm>` distance, `o` / `c` open / close by that distance, `h` back to zero, `z` set zero, `?` menu. Any key cancels a move. |
| [`single_driver_test`](firmware/legacy/single_driver_test/single_driver_test.ino) | One driver at a time, for bring-up | `a` / `b` select the driver, `f` / `r` one revolution forward / reverse, `t` back-and-forth test ×3, `s<steps/s>` revolution rate, plus the same trial commands |

Limitations:
- **Motion only**: no load cell, no load or travel-limit stops, no calibration in EEPROM.
- **Not kept in sync** with `vsbc_firmware`: their rate limit and direction settings are constants in each sketch.
- **The Pi app can't use them.** Connecting reports "No answer from vsbc_firmware", shows what the board printed (e.g. `Sketch: dual_stepper_mega`), and points to the Firmware tab. Upload `firmware/vsbc_firmware` again before using the app.

The same jobs in `vsbc_firmware`:

| Legacy | `vsbc_firmware` |
|---|---|
| `v0.5` then `d20` then `o` / `c` | `v0.5` then `m20` / `m-20` |
| `h`, `z`, `?` | `h`, `z`, `?` (the same) |
| `a` / `b` (select a driver) | `MOTORS A` / `MOTORS B` (`MOTORS AB` for both again) |
| `f` / `r` (one revolution) | `m5` / `m-5` (one revolution = 5 mm) |
| `s4000` (steps/s) | `v6.25` (mm/s = steps/s ÷ 640) |
| `t` (back and forth ×3) | no single command: repeat `m5` / `m-5` |

---

## Configuration

**App settings** (`config.toml`, optional): copy [`config.example.toml`](config.example.toml) to `config.toml` in the repository folder, or to `~/.config/vsbc/config.toml`. It sets:
- the serial port and baud rate;
- the output folder;
- where the load-cell amplifier is wired (`source = "pi"` or `"mega"`) and the Pi GPIO pins;
- the load-cell capacity, local gravity and the HX711's sample rate (`rate_hz`: 10 as shipped, 80 with the RATE jumper cut);
- default test settings;
- the watchdog timeout;
- GUI refresh rate, text size (`font_pt`, and `scale` to override the automatic screen scaling) and full screen;
- `[units]`: the default display units (the Units tab overrides them and remembers its choice);
- `[storage]`: the free-space warning levels;
- `[camera]`: the camera folder and environment, recording with each test, how frames are saved (`save_mode`), the load-cell delay, and what runs after the test.

**Camera settings** (exposure, crop, frame rate, RAM buffer, DIC parameters) are the USER SETTINGS in [`camera/capture_dic_live.py`](camera/capture_dic_live.py), not in the config file.

**Machine calibration** (steps/mm, load scale, travel limits, overload cutoff, directions): stored on the Mega and changed from the Calibrate tab or with `SET` + `SAVE`. It is not in the config file.

**Firmware constants** ([`config.h`](firmware/vsbc_firmware/config.h)):
- pins;
- `STEPS_PER_REV` (must match SW5–SW8) and `SCREW_LEAD_MM`;
- `MAX_MOTOR_RPM` (120), which caps the rate at 10 mm/s;
- the HX711 pins and gain, for an HX711 wired to the Mega;
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
  - **load cell lost**: during a test, no load reading arrives for 0.5 s. With the HX711 on the Pi this also covers a frozen app or an unplugged amplifier;
  - **watchdog**: the app has sent nothing for 3 s, e.g. the Pi crashed or the cable was unplugged.
- **Load readings travel over USB** when the HX711 is on the Pi. That adds a few milliseconds before the Mega sees a reading, and a discarded reading (see [docs/hardware.md](docs/hardware.md)) adds one sample period. Both are small next to the two-sample break detection, but they are not zero.
- **No acceleration ramp.** Moves start at full rate on purpose, to keep the displacement rate constant from the first step. The 120 RPM cap keeps starts reliable; [docs/hardware.md](docs/hardware.md) explains why.
- **Service mode** (one motor only) racks the crosshead if the screws are coupled. Use it for bring-up only.

---

## Hardware bring-up checklist

1. **Drivers**: S2 = 5V and DIP = ON OFF OFF ON ON ON OFF ON on **both** drivers. Motor connected before power-up.
2. **Firmware**: upload from the Arduino IDE or `vsbc flash`. In the Serial Monitor, `?` should print the menu. `STATUS` shows `lc=1` only if an HX711 is wired to the Mega; with the HX711 on the Pi, `lc=0` is expected here. With `lc=0`, `?` says why (no conversions, or readings not answered).
3. **One motor at a time**, screws uncoupled if possible: `MOTORS A`, `Z`, `V1`, `M5`. Expect 3200 steps, 5.0 mm of screw travel in 5.0 s. Then `MOTORS B` and repeat.
4. **Both motors**: `MOTORS AB`, `M5`. Both screws move together, the same distance and direction. If B turns the wrong way, `SET invert_b 1`, `SAVE`.
5. **Cancel**: `V0.5`, `M20`, then type `x` + Enter mid-move. It stops at once, and `H` returns to 0.
6. **Load cell**: `vsbc loadcell`. Expect about 10 readings per second (80 if the RATE jumper is cut: see [docs/hardware.md](docs/hardware.md#hx711-sample-rate)) and counts that change when you press on the load cell. With the HX711 on the Pi, also few discarded readings (a few percent at most).
7. **App**: on the Pi, run `vsbc`, connect, then do the calibration steps in [Calibration](#calibration) in order.
8. **Safety tests**, with a low `max_load`:
   - press on the load cell during a jog: `OVERLOAD` stop;
   - unplug the HX711 during a `RUN`: `LOADCELL` stop;
   - kill the app during a move: `WATCHDOG` stop within 3 s (during a `RUN` with the HX711 on the Pi, `LOADCELL` comes first, after 0.5 s).
9. **First test**: a sacrificial specimen at a slow rate. Check that the elapsed time matches distance ÷ rate.

---

## Development

```bash
pip install -e ".[gui,dev]"
pytest                          # 136 tests against the simulators (incl. offscreen GUI runs and the
                                # camera process with the simulated camera), ~80 s
vsbc gui --sim                  # try GUI changes without hardware (simulated Mega and camera)
vsbc flash --compile-only       # compile the firmware (needs arduino-cli)
```

- The camera tests and `--sim` run the camera process with this environment's Python and the simulated camera. With `opencv-python-headless` installed they also make the video.
- The camera process and the post-processing jobs import only the standard library and NumPy (plus pypylon, cv2 and pyvale in the camera environment). Never import cv2, pypylon or pyvale in the GUI process.

- The protocol is defined in [docs/protocol.md](docs/protocol.md). The firmware (`commands.cpp`, `tester.cpp`, `motion.cpp`) and the simulator (`vsbc/sim.py`) both implement it. **Change all three together**; the simulator is what the tests run against.
- Both the firmware and the Python package carry a version number. The protocol version must match exactly (the app refuses to connect otherwise); a firmware version mismatch only gives a warning.
- The Mega sketch is split into modules; the Arduino IDE shows each file as a tab:
  - `motion.*`: Timer1 step engine;
  - `loadcell_hx711.cpp`: HX711 wired to the Mega;
  - `tester.*`: load samples from the host or the Mega's HX711, safety, averaging, telemetry;
  - `commands.*`: parser;
  - `settings.*`: EEPROM.
- The whole firmware uses about 26 kB of flash and 1.2 kB of RAM on the Mega 2560.

---

## Known limitations / TODO

- [ ] **Hardware: set SW5–8 to ON ON OFF ON (3200 pulses/rev) on both drivers.** The older 400 (OFF ON ON ON) and 25000 (all OFF) settings move the wrong distance; the travel calibration flags them.
- [ ] **Hardware: set S2 to 5V on both drivers.** It ships at 24V.
- [ ] **Choose the load cell and wire the SparkFun HX711 to the Mega** (VCC and VDD → 5V, DAT → D26, CLK → D27). Put the load cell's capacity in `config.toml`.
- [ ] **If the HX711 is moved to the Pi, measure its timing on the Pi 5** with `vsbc loadcell`. The reader is tested against a fake GPIO only; the share of discarded readings on the real Pi, with the GUI running, is not known yet.
- [ ] **Run the new firmware on the real Mega.** Version 2.3.0 (protocol 3: sample times to the microsecond, 2 s TARE/CAL averaging) compiles without warnings for `arduino:avr:mega` (avr-gcc 7.3, `-Wall -Wextra`). Upload it with `vsbc flash` or the Firmware tab: this app refuses firmware that speaks protocol 2. The app is tested against the simulator, but neither has run on the hardware.
- [ ] **Try the GUI on the Pi 5** (tested on Windows).
- [ ] **Set real travel limits and the overload cutoff** after the first calibration.
- [ ] **Fit a hardware E-stop** that cuts motor power; optionally limit switches (pins in `config.h`).
- [ ] **Check motor temperature.** SW1–3 at ON OFF OFF gives 3.76 A peak / 2.69 A RMS, just under the motor's 3.0 A rating, with full current at standstill (SW4 ON). Lower it if the motors run too hot.
- [ ] Extension is measured at the crosshead, so it includes frame and grip compliance. A compliance correction or an extensometer would give true specimen strain. (The DIC strain from the camera does not have this problem.)
- [ ] Only pull-to-failure is built in. Cyclic, hold/relaxation and compression tests can follow the same pattern (`RUN` with a negative distance already moves in compression).
- [ ] **Camera on the Pi:**
  - Run `bash scripts/install_pi.sh --camera`, reboot, then `vsbc camera check`.
  - Expect 0 dropped frames at 478 fps over USB. Check the disk write speed: 1 frame per load sample needs about 7.7 MB/s at 10/s (61 MB/s at 80/s), so use a USB 3 SSD for `output_dir` if the SD card is slower.
  - Check that `vsbc camera check` runs its small synthetic DIC with pyvale 2026.10.2 on the Pi.
  - The camera integration is tested with the simulated camera only.
- [ ] **Measure the load-cell delay** with a tap test (tap the grip in view; compare the video and data.csv) and set `load_delay_ms`.
- [ ] Optional: a faster ADC than the HX711 (NAU7802 up to 320 samples/s, ADS1220 up to 2 kHz) would give more frames paired with load readings.

---

## References

Similar open-source testers looked at for this design:

- [CNCKitchen/Open-Pull](https://github.com/CNCKitchen/Open-Pull): Arduino + HX711 + two steppers, controlled from a serial terminal. It shows why the load cell and motion both belong on the microcontroller.
- [FrancescoNegri/universal-testing-machine](https://github.com/FrancescoNegri/universal-testing-machine): a Raspberry Pi-only tester. The idea for timestamped JSON calibration records comes from here. It reads the HX711 from Linux, as this design can too (`source = "pi"`).
- Raspberry Pi forum threads on HX711 bit-banging from Linux ([random values](https://forums.raspberrypi.com/viewtopic.php?t=322131), [hanging](https://forums.raspberrypi.com/viewtopic.php?t=370007)) report occasional bad readings, and note that RPi.GPIO does not run on a Pi 5. This is why the reader uses lgpio, times every clock pulse and discards a reading that took too long, why the safety decisions stay on the Mega, and why the HX711 can still be wired to the Mega.
- [MrYsLab/telemetrix](https://github.com/MrYsLab/telemetrix): a Firmata-style "Python drives the Arduino" library. Its steppers are timed from `loop()`, which is not precise enough for constant-rate lockstep, so this design keeps its own Timer1 engine and protocol.
- [Real-Time-Py-Serial-Plotter](https://github.com/iskandarputra/Real-Time-Py-Serial-Plotter): the PyQt5 + pyqtgraph pattern used for the live plots.
- [Arduino CLI on a Raspberry Pi](https://siytek.com/arduino-cli-raspberry-pi/): how `vsbc flash` compiles and uploads.
- [Instron: tensile testing guide](https://www.instron.com/en/resources/test-types/tensile-test/): end-of-test by % load drop from peak; size the load cell so peak loads are 10–90 % of its capacity.

---

## Team

MEEN 402 — Texas A&M University

_TODO: team members, advisor, semester._
