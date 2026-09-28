#!/usr/bin/env python3
"""
Serial terminal + logger for the VSBC Arduino Mega sketches.

Use this instead of the Arduino Serial Monitor (only one program can have the
port open at a time). It shows everything the Mega prints, sends what you type
when you press Enter, and saves the whole session to

    Outputs/<YYYY-MM-DD_HH-MM-SS>_<sketch>.log

where the date/time is when the session started and <sketch> is the name the
sketch prints at startup ("Sketch: dual_stepper_mega" or
"Sketch: single_driver_test"). Every log line is stamped with the PC time;
commands you send are logged as "> command".

Usage:
    pip install pyserial
    python serial_logger.py              # auto-detect the Mega
    python serial_logger.py --port COM5  # or name the port

Cancel a trial as usual: type x and press Enter.
Ctrl+C (or typing "quit") sends a cancel to the Mega, saves the log and exits.
"""

import argparse
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import serial
from serial.tools import list_ports

BAUD = 115200
OUTPUT_DIR = Path(__file__).resolve().parent / "Outputs"

SKETCH_PREFIX = "Sketch: "
SKETCH_WAIT_S = 4.0      # opening the port resets the Mega; wait for its banner
MENU_WAIT_S = 2.0        # if no banner arrives, ask for the menu and wait this long

# USB vendor IDs: Arduino, Arduino.org, WCH CH340 (common on Mega clones)
ARDUINO_VIDS = {0x2341, 0x2A03, 0x1A86}
QUIT_WORDS = {"quit", "exit"}


def find_port():
    """Pick the Mega's serial port, or exit with a list of ports to choose from."""
    ports = list(list_ports.comports())
    arduino = [p for p in ports if p.vid in ARDUINO_VIDS]
    if len(arduino) == 1:
        return arduino[0].device
    if len(ports) == 1:
        return ports[0].device

    if not ports:
        sys.exit("No serial ports found. Is the Mega plugged in?")
    print("Could not pick a serial port automatically. Available ports:")
    for p in ports:
        print(f"  {p.device}  {p.description}")
    sys.exit("Run again with --port <name>, e.g. --port COM5")


class SessionLog:
    """Timestamped log. Lines are buffered until the file name (which needs the
    sketch name) is known, then written straight through."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buffer = []
        self._file = None

    def write(self, text):
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {text}\n"
        with self._lock:
            if self._file:
                self._file.write(line)
                self._file.flush()
            else:
                self._buffer.append(line)

    def open(self, path, header_lines):
        with self._lock:
            self._file = open(path, "w", encoding="utf-8")
            for header in header_lines:
                self._file.write(f"# {header}\n")
            self._file.writelines(self._buffer)
            self._buffer.clear()
            self._file.flush()

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


def read_serial(ser, log, sketch_found, state, stop):
    """Background thread: echo and log every line from the Mega."""
    while not stop.is_set():
        try:
            raw = ser.readline()
        except serial.SerialException as e:
            print(f"\nSerial error: {e}. Press Enter to exit.")
            log.write(f"# serial error: {e}")
            stop.set()
            break
        if not raw:
            continue

        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        print(text, flush=True)
        log.write(text)

        if not sketch_found.is_set() and text.startswith(SKETCH_PREFIX):
            state["sketch"] = text[len(SKETCH_PREFIX):].strip()
            sketch_found.set()


def main():
    parser = argparse.ArgumentParser(description="Serial terminal + logger for the VSBC Mega sketches.")
    parser.add_argument("--port", help="serial port, e.g. COM5 (default: auto-detect)")
    parser.add_argument("--baud", type=int, default=BAUD, help=f"baud rate (default {BAUD})")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR, help="folder for log files (default: Outputs/)")
    args = parser.parse_args()

    port = args.port or find_port()
    start = datetime.now()

    try:
        ser = serial.Serial(port, args.baud, timeout=0.1)
    except serial.SerialException as e:
        sys.exit(f"Could not open {port}: {e}\n(Close the Arduino Serial Monitor if it is open.)")

    print(f"Connected to {port} at {args.baud} baud. Waiting for the sketch to start...")

    log = SessionLog()
    state = {}
    sketch_found = threading.Event()
    stop = threading.Event()
    reader = threading.Thread(target=read_serial, args=(ser, log, sketch_found, state, stop), daemon=True)
    reader.start()

    if not sketch_found.wait(SKETCH_WAIT_S):
        # The Mega did not reset on connect (or missed the banner): ask for the menu.
        ser.write(b"?\n")
        log.write("> ?")
        sketch_found.wait(MENU_WAIT_S)

    sketch = re.sub(r"[^A-Za-z0-9_.-]", "_", state.get("sketch", "unknown_sketch"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"{start:%Y-%m-%d_%H-%M-%S}_{sketch}.log"
    log.open(path, [
        "VSBC serial log",
        f"Sketch:  {sketch}",
        f"Started: {start:%Y-%m-%d %H:%M:%S}",
        f"Port:    {port} @ {args.baud} baud",
    ])

    print(f"Logging to {path}")
    print('Type commands and press Enter. Ctrl+C or "quit" to exit.')

    try:
        while not stop.is_set():
            line = input()
            if stop.is_set() or line.strip().lower() in QUIT_WORDS:
                break
            ser.write((line + "\n").encode("utf-8"))
            log.write(f"> {line}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        # Stop any trial still running before letting go of the port. The
        # sketches ignore x when idle, so this is harmless otherwise.
        try:
            ser.write(b"x\n")
            log.write("> x   (sent on exit to cancel any running trial)")
            ser.flush()
            time.sleep(0.3)          # let the cancel report arrive and be logged
        except serial.SerialException:
            pass
        stop.set()
        reader.join(timeout=1.0)
        log.write("# session ended")
        ser.close()
        log.close()
        print(f"\nLog saved to {path}")


if __name__ == "__main__":
    main()
