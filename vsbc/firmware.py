"""Compile and upload firmware/vsbc_firmware with arduino-cli, so the Pi can
flash its own Mega without the Arduino IDE.

Install arduino-cli once (scripts/install_pi.sh does this):
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=~/.local/bin sh
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from vsbc.config import FIRMWARE_DIR

FQBN = "arduino:avr:mega:cpu=atmega2560"
CLI_ENV = "VSBC_ARDUINO_CLI"


class FirmwareToolError(RuntimeError):
    pass


def find_arduino_cli(explicit: str | None = None) -> str:
    candidates = [explicit, os.environ.get(CLI_ENV), shutil.which("arduino-cli"),
                  str(Path.home() / ".local" / "bin" / "arduino-cli"),
                  str(Path.home() / "bin" / "arduino-cli")]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise FirmwareToolError(
        "arduino-cli not found. Install it (see scripts/install_pi.sh) or set "
        f"{CLI_ENV} to its path. The Arduino IDE also works: open firmware/vsbc_firmware."
    )


def _run(args: list[str], log: Callable[[str], None]) -> None:
    log("$ " + " ".join(args))
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    if proc.wait() != 0:
        raise FirmwareToolError(f"{Path(args[0]).name} {args[1]} failed (exit code {proc.returncode})")


def ensure_avr_core(cli: str, log: Callable[[str], None] = print) -> None:
    out = subprocess.run([cli, "core", "list", "--format", "json"], capture_output=True, text=True)
    installed = False
    try:
        data = json.loads(out.stdout or "{}")
        platforms = data.get("platforms", data) if isinstance(data, dict) else data
        installed = any(p.get("id") == "arduino:avr" for p in platforms or [])
    except (ValueError, AttributeError):
        installed = "arduino:avr" in (out.stdout or "")
    if not installed:
        log("Installing the Arduino AVR core (one time)...")
        _run([cli, "core", "update-index"], log)
        _run([cli, "core", "install", "arduino:avr"], log)


def compile_firmware(cli: str | None = None, log: Callable[[str], None] = print) -> None:
    cli = find_arduino_cli(cli)
    ensure_avr_core(cli, log)
    _run([cli, "compile", "--fqbn", FQBN, str(FIRMWARE_DIR)], log)


def upload_firmware(port: str, cli: str | None = None, log: Callable[[str], None] = print) -> None:
    """Compile and upload. The serial port must not be open elsewhere."""
    cli = find_arduino_cli(cli)
    ensure_avr_core(cli, log)
    _run([cli, "compile", "--fqbn", FQBN, str(FIRMWARE_DIR)], log)
    _run([cli, "upload", "-p", port, "--fqbn", FQBN, str(FIRMWARE_DIR)], log)
    log("Upload finished.")
