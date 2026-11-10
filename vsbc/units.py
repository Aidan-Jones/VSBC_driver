"""Display units for force and travel.

The firmware, the protocol and the data files work in newtons and millimetres.
`Units` converts to and from what the operator picked in the Display tab (or
[units] in config.toml), for the GUI, summary.txt, plot.png, the extra data.csv
columns and the camera video overlay. Derived values follow the choice: rates
are length per minute (test) or per second (jog), stiffness is force per
length, and stress and energy switch to psi and in·lbf when the force is lbf.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

N_PER = {"N": 1.0, "lbf": 4.4482216152605}            # newtons per unit
MM_PER = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}   # millimetres per unit
FORCE_UNITS = tuple(N_PER)
LENGTH_UNITS = tuple(MM_PER)

# Digits that keep the resolution of the readings: 0.1 um of position, 0.01 N of load.
LENGTH_DIGITS = {"mm": 4, "cm": 5, "m": 7, "in": 5}
FORCE_DIGITS = {"N": 2, "lbf": 3}
PSI_PER_MPA = 145.0377377
IN_LBF_PER_J = 8.850745791


@dataclass(frozen=True)
class Units:
    force: str = "N"
    length: str = "mm"

    def __post_init__(self):
        if self.force not in N_PER:
            raise ValueError(f"force unit must be one of {FORCE_UNITS}, not {self.force!r}")
        if self.length not in MM_PER:
            raise ValueError(f"length unit must be one of {LENGTH_UNITS}, not {self.length!r}")

    @property
    def is_default(self) -> bool:
        return self.force == "N" and self.length == "mm"

    # ------------------------------------------------------------ force --
    def force_from_n(self, newtons: float) -> float:
        return newtons / N_PER[self.force]

    def force_to_n(self, value: float) -> float:
        return value * N_PER[self.force]

    @property
    def force_digits(self) -> int:
        return FORCE_DIGITS[self.force]

    # ----------------------------------------------------------- length --
    def length_from_mm(self, mm: float) -> float:
        return mm / MM_PER[self.length]

    def length_to_mm(self, value: float) -> float:
        return value * MM_PER[self.length]

    @property
    def length_digits(self) -> int:
        return LENGTH_DIGITS[self.length]

    def area_from_mm2(self, mm2: float) -> float:
        return mm2 / MM_PER[self.length] ** 2

    def area_to_mm2(self, value: float) -> float:
        return value * MM_PER[self.length] ** 2

    # ---------------------------------------------------------- derived --
    @property
    def rate_min_label(self) -> str:
        return f"{self.length}/min"

    @property
    def rate_s_label(self) -> str:
        return f"{self.length}/s"

    @property
    def area_label(self) -> str:
        return f"{self.length}²"

    @property
    def stiffness_label(self) -> str:
        return f"{self.force}/{self.length}"

    def stiffness_from_n_per_mm(self, k: float) -> float:
        return k * MM_PER[self.length] / N_PER[self.force]

    @property
    def stress_label(self) -> str:
        return "psi" if self.force == "lbf" else "MPa"

    def stress_from_mpa(self, mpa: float) -> float:
        return mpa * PSI_PER_MPA if self.force == "lbf" else mpa

    @property
    def energy_label(self) -> str:
        return "in·lbf" if self.force == "lbf" else "J"

    def energy_from_j(self, joules: float) -> float:
        return joules * IN_LBF_PER_J if self.force == "lbf" else joules

    def format_force(self, newtons: float | None, digits: int | None = None) -> str:
        if newtons is None or not math.isfinite(newtons):
            return "—"
        return f"{self.force_from_n(newtons):.{self.force_digits if digits is None else digits}f} {self.force}"

    def format_length(self, mm: float | None, digits: int | None = None) -> str:
        if mm is None or not math.isfinite(mm):
            return "—"
        return f"{self.length_from_mm(mm):.{self.length_digits if digits is None else digits}f} {self.length}"

    def to_dict(self) -> dict:
        return asdict(self)


def units_from_dict(data, fallback: Units | None = None) -> Units:
    """Units from {"force": ..., "length": ...}; the fallback (SI) for anything missing or invalid."""
    base = fallback or Units()
    if not isinstance(data, dict):
        return base
    try:
        return Units(data.get("force", base.force), data.get("length", base.length))
    except ValueError:
        return base


def load_preferences(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_preferences(path: Path, updates: dict) -> None:
    """Merge `updates` into the preferences file (the GUI choices kept between sessions)."""
    path = Path(path)
    data = load_preferences(path)
    data.update(updates)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("could not save the preferences: %s", e)


def effective_units(config) -> Units:
    """The Display tab's last choice (Outputs/preferences.json), else [units] in config.toml."""
    base = Units(config.units.force, config.units.length)
    return units_from_dict(load_preferences(config.preferences_file).get("units"), base)


def save_units(config, units: Units) -> None:
    save_preferences(config.preferences_file, {"units": units.to_dict()})


def clear_saved_units(config) -> None:
    data = load_preferences(config.preferences_file)
    if data.pop("units", None) is not None:
        try:
            config.preferences_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("could not save the preferences: %s", e)
