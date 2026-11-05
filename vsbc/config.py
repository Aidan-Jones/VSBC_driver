"""Host configuration.

Machine calibration (steps/mm, load-cell scale, travel limits) lives in the
Mega's EEPROM, not here. This only holds host preferences, test defaults,
where the load-cell amplifier is wired, the camera, display units and the
storage warning levels.
Values come from an optional TOML file (see config.example.toml), searched in
this order: --config, $VSBC_CONFIG, <repo>/config.toml, ~/.config/vsbc/config.toml.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from vsbc.units import FORCE_UNITS, LENGTH_UNITS

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = REPO_ROOT / "firmware" / "vsbc_firmware"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "Outputs"
CONFIG_ENV = "VSBC_CONFIG"
LOADCELL_SOURCES = ("pi", "mega")
CAMERA_SAVE_MODES = ("match", "every", "all")


@dataclass
class SerialConfig:
    port: str | None = None          # None = auto-detect the Mega
    baud: int = 115200
    ready_timeout_s: float = 4.0     # opening the port resets the Mega
    log_data_lines: bool = False     # also write every D line to the session log


@dataclass
class LoadCellConfig:
    source: str = "mega"             # "pi": HX711 on the Pi's GPIO pins; "mega": HX711 on the Mega
    dout_pin: int = 5                # Pi GPIO (BCM number) wired to the HX711 DAT pin
    sck_pin: int = 6                 # Pi GPIO (BCM number) wired to the HX711 CLK pin
    gain: int = 128                  # HX711 gain: 128 or 64 (channel A), 32 (channel B)
    rate_hz: float = 10.0            # HX711 conversions per second: 10 as shipped, 80 with its RATE jumper cut
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
    font_pt: float = 11.0            # text size on a 1280 × 800 screen; other screens scale it
    scale: float = 0.0               # 0 = from the screen size; else a fixed factor on font_pt


@dataclass
class CameraConfig:
    """The DIC camera. Camera and DIC parameters (exposure, crop, buffer, subsets) are the
    USER SETTINGS in camera/capture_dic_live.py; these are the app's choices around them."""

    enabled: bool = True             # show the Camera tab
    auto_start: bool = True          # start the camera preview when the app opens
    dir: str = "camera"              # folder with the camera scripts, relative to the repository
    python: str = ""                 # camera environment's Python; "" = .venv-camera, else this app's
    simulate: bool = False           # synthetic camera (also with --sim)
    record_with_test: bool = True
    save_mode: str = "match"         # "match": the frame nearest each load sample; "every": SAVE_EVERY; "all"
    match_divisor: int = 1           # "match": one frame every this many load samples
    dump_on_break: bool = True       # keep every frame of the RAM buffer when the Mega detects the break
    load_delay_ms: float = 0.0       # HX711 filter lag, used when pairing frames with load samples
    live_dic: bool = False           # DIC during the test (competes with grabbing on the Pi)
    run_dic: bool = True             # after the test
    run_strain: bool = True
    strain_animation: bool = False
    crack_tip_image: bool = True
    crack_tip_opening: bool = True
    make_video: bool = True
    video_fps: float = 30.0


@dataclass
class UnitsConfig:
    force: str = "N"                 # "N" or "lbf"
    length: str = "mm"               # "mm", "cm", "m" or "in"


@dataclass
class StorageConfig:
    warn_free_gb: float = 10.0       # banner when less than this is free where the outputs are saved
    critical_free_gb: float = 2.0


@dataclass
class Config:
    serial: SerialConfig = field(default_factory=SerialConfig)
    loadcell: LoadCellConfig = field(default_factory=LoadCellConfig)
    test: TestDefaults = field(default_factory=TestDefaults)
    gui: GuiConfig = field(default_factory=GuiConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    units: UnitsConfig = field(default_factory=UnitsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
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

    @property
    def pending_load_file(self) -> Path:
        """Load-cell calibration made without the Mega, waiting to be stored on it."""
        return self.output_dir / "pending_load_calibration.json"

    @property
    def preferences_file(self) -> Path:
        """Choices made in the GUI that are kept between sessions (units)."""
        return self.output_dir / "preferences.json"

    @property
    def camera_dir(self) -> Path:
        path = Path(self.camera.dir).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def camera_recordings_dir(self) -> Path:
        """Camera recordings made without a test."""
        return self.output_dir / "camera"


def _apply(obj, data: dict, where: str = "") -> None:
    for key, value in data.items():
        # Config.source (the file that was read) is not a setting; loadcell.source is.
        if (key == "source" and not where) or not hasattr(obj, key):
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

    if cfg.loadcell.source not in LOADCELL_SOURCES:
        raise ValueError(f"config key 'loadcell.source' must be one of {LOADCELL_SOURCES}, not {cfg.loadcell.source!r}")
    if not cfg.loadcell.rate_hz > 0:
        raise ValueError(f"config key 'loadcell.rate_hz' must be above 0, not {cfg.loadcell.rate_hz!r}")
    if cfg.camera.save_mode not in CAMERA_SAVE_MODES:
        raise ValueError(f"config key 'camera.save_mode' must be one of {CAMERA_SAVE_MODES}, not {cfg.camera.save_mode!r}")
    if cfg.units.force not in FORCE_UNITS:
        raise ValueError(f"config key 'units.force' must be one of {FORCE_UNITS}, not {cfg.units.force!r}")
    if cfg.units.length not in LENGTH_UNITS:
        raise ValueError(f"config key 'units.length' must be one of {LENGTH_UNITS}, not {cfg.units.length!r}")

    out = Path(cfg.output_dir).expanduser()
    if not out.is_absolute():
        out = (cfg.source.parent if cfg.source else REPO_ROOT) / out
    cfg.output_dir = out
    return cfg
