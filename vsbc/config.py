"""Host configuration.

Machine calibration (steps/mm, load-cell scale, travel limits) lives in the
Mega's EEPROM, not here. This only holds host preferences and test defaults.
Values come from an optional TOML file (see config.example.toml), searched in
this order: --config, $VSBC_CONFIG, <repo>/config.toml, ~/.config/vsbc/config.toml.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = REPO_ROOT / "firmware" / "vsbc_firmware"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "Outputs"
CONFIG_ENV = "VSBC_CONFIG"


@dataclass
class SerialConfig:
    port: str | None = None          # None = auto-detect the Mega
    baud: int = 115200
    ready_timeout_s: float = 4.0     # opening the port resets the Mega
    log_data_lines: bool = False     # also write every D line to the session log


@dataclass
class LoadCellConfig:
    capacity_n: float = 500.0        # rated capacity; sets the default max_load
    gravity: float = 9.80665         # m/s^2, converts calibration masses to N


@dataclass
class TestDefaults:
    rate_mm_min: float = 5.0
    max_extension_mm: float = 50.0
    stop_load_n: float = 0.0         # 0 = off
    break_drop_pct: float = 40.0
    break_min_n: float = 5.0
    tare_before: bool = True
    return_after: bool = False
    jog_rate_mm_s: float = 2.0


@dataclass
class GuiConfig:
    fullscreen: bool = False
    refresh_hz: float = 25.0
    time_window_s: float = 60.0
    font_pt: int = 11


@dataclass
class Config:
    serial: SerialConfig = field(default_factory=SerialConfig)
    loadcell: LoadCellConfig = field(default_factory=LoadCellConfig)
    test: TestDefaults = field(default_factory=TestDefaults)
    gui: GuiConfig = field(default_factory=GuiConfig)
    output_dir: Path = DEFAULT_OUTPUT_DIR
    watchdog_ms: int = 3000          # firmware stops a move if the host is silent this long
    ping_interval_s: float = 0.5
    source: Path | None = None       # file this config was read from, if any

    @property
    def tests_dir(self) -> Path:
        return self.output_dir / "tests"

    @property
    def calibration_dir(self) -> Path:
        return self.output_dir / "calibration"

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def state_file(self) -> Path:
        return self.output_dir / "last_position.json"


def _apply(obj, data: dict, where: str = "") -> None:
    for key, value in data.items():
        if key == "source" or not hasattr(obj, key):
            raise ValueError(f"unknown config key '{where}{key}'")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config key '{where}{key}' must be a table")
            _apply(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    """Defaults, overridden by the first config file found."""
    cfg = Config()
    if path is not None:
        candidates = [Path(path)]
        if not candidates[0].is_file():
            raise FileNotFoundError(f"config file not found: {path}")
    else:
        env = os.environ.get(CONFIG_ENV)
        candidates = [Path(env)] if env else []
        candidates += [REPO_ROOT / "config.toml", Path.home() / ".config" / "vsbc" / "config.toml"]

    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, "rb") as f:
                _apply(cfg, tomllib.load(f))
            cfg.source = candidate
            break

    out = Path(cfg.output_dir).expanduser()
    if not out.is_absolute():
        out = (cfg.source.parent if cfg.source else REPO_ROOT) / out
    cfg.output_dir = out
    return cfg
