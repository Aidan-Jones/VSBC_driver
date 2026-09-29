"""Load-cell and travel calibration: the maths, data collection and records.

The GUI wizards and the CLI prompts both use these functions. Results are
written to the firmware (SET + SAVE, so the EEPROM stays the source of truth)
and a timestamped JSON record is kept in Outputs/calibration/.
"""

from __future__ import annotations

import json
import math
import queue
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from vsbc.protocol import Sample

G_STANDARD = 9.80665

# Pulses/rev settings of the DM542T V4.0 (SW5-SW8). Used to recognise a travel
# error that is really a DIP-switch mismatch.
DM542T_PULSES_PER_REV = (200, 400, 800, 1600, 3200, 6400, 12800, 25600,
                         1000, 2000, 4000, 5000, 8000, 10000, 20000, 25000)
FIRMWARE_PULSES_PER_REV = 400


# ------------------------------------------------------------------ load cell --

@dataclass
class LoadPoint:
    force_n: float
    raw_mean: float
    raw_std: float
    n: int
    label: str = ""


@dataclass
class LoadFit:
    scale: float                 # counts per newton (sign included)
    offset: float                # counts at zero load
    r2: float
    residuals_n: list[float]
    max_residual_n: float
    max_residual_pct_fs: float | None


def mass_to_newtons(value: float, unit: str, g: float = G_STANDARD) -> float:
    """Convert a known mass or force to newtons. unit: g, kg, lb, N or kN."""
    u = unit.strip().lower()
    if u == "g":
        return value / 1000.0 * g
    if u == "kg":
        return value * g
    if u in ("lb", "lbs"):
        return value * 0.45359237 * g
    if u == "n":
        return value
    if u == "kn":
        return value * 1000.0
    raise ValueError(f"unknown unit {unit!r} (use g, kg, lb, N or kN)")


def parse_load(text: str, g: float = G_STANDARD) -> float:
    """'500 g', '2kg', '10 N' -> newtons. A bare number is taken as N."""
    m = re.fullmatch(r"\s*([-+]?\d+(?:\.\d*)?|[-+]?\.\d+)\s*([a-zA-Z]*)\s*", text)
    if not m:
        raise ValueError(f"cannot read {text!r}; try '500 g', '2 kg' or '10 N'")
    return mass_to_newtons(float(m.group(1)), m.group(2) or "N", g)


def fit_load_calibration(points: list[LoadPoint], capacity_n: float | None = None) -> LoadFit:
    """Least-squares fit raw = offset + scale * force."""
    forces = np.array([p.force_n for p in points], dtype=float)
    raws = np.array([p.raw_mean for p in points], dtype=float)
    if len(points) < 2 or np.ptp(forces) == 0:
        raise ValueError("need at least two points with different loads (include the zero-load point)")
    scale, offset = np.polyfit(forces, raws, 1)
    if scale == 0:
        raise ValueError("the load cell did not respond to the applied loads")
    predicted = offset + scale * forces
    ss_res = float(np.sum((raws - predicted) ** 2))
    ss_tot = float(np.sum((raws - raws.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    residuals = ((raws - predicted) / scale).tolist()
    max_res = float(np.max(np.abs(residuals)))
    pct = 100.0 * max_res / capacity_n if capacity_n else None
    return LoadFit(float(scale), float(offset), r2, residuals, max_res, pct)


def collect_raw(machine, seconds: float = 3.0, min_samples: int = 5) -> tuple[float, float, int]:
    """Average the raw load-cell counts streamed over `seconds`. Returns (mean, std, n)."""
    q: queue.Queue[int] = queue.Queue()

    def listener(msg):
        if isinstance(msg, Sample) and msg.raw != 0:
            q.put(msg.raw)

    machine.add_listener(listener)
    try:
        values = []
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and len(values) >= min_samples:
                break
            if remaining < -5.0:
                raise TimeoutError("no load-cell samples arriving (is the load cell connected?)")
            try:
                values.append(q.get(timeout=max(remaining, 0.2)))
            except queue.Empty:
                continue
    finally:
        machine.remove_listener(listener)
    arr = np.array(values, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=1)) if len(arr) > 1 else 0.0, len(arr)


def apply_load_fit(machine, fit: LoadFit) -> None:
    machine.set("load_scale", fit.scale)
    machine.set("load_offset", int(round(fit.offset)))
    machine.save()


# --------------------------------------------------------------------- travel --

@dataclass
class TravelTrial:
    commanded_mm: float
    measured_mm: float


@dataclass
class TravelResult:
    old_steps_per_mm: float
    new_steps_per_mm: float
    ratio: float                 # measured / commanded
    warnings: list[str] = field(default_factory=list)


def travel_correction(old_steps_per_mm: float, trials: list[TravelTrial]) -> TravelResult:
    """New steps/mm from commanded vs measured travel: steps_per_mm *= commanded / measured."""
    usable = [t for t in trials if t.commanded_mm != 0 and t.measured_mm != 0]
    if not usable:
        raise ValueError("need at least one trial with non-zero commanded and measured travel")
    ratio = float(np.mean([t.measured_mm / t.commanded_mm for t in usable]))
    if ratio <= 0:
        raise ValueError("measured travel has the opposite sign: check the direction first")
    warnings = []
    if abs(ratio - 1.0) > 0.05:
        warnings.append(
            f"Measured travel is {abs(ratio - 1) * 100:.1f} % off. That is more than screw-lead "
            "error: check the microstep DIP switches (SW5-SW8, both drivers) and the screw lead "
            "before saving."
        )
        for pulses in DM542T_PULSES_PER_REV:
            expected = FIRMWARE_PULSES_PER_REV / pulses
            if pulses != FIRMWARE_PULSES_PER_REV and abs(ratio / expected - 1) < 0.03:
                warnings.append(
                    f"The ratio matches drivers set to {pulses} pulses/rev; the firmware "
                    f"assumes {FIRMWARE_PULSES_PER_REV}."
                )
    if len(usable) > 1:
        spread = max(t.measured_mm / t.commanded_mm for t in usable) - min(t.measured_mm / t.commanded_mm for t in usable)
        if spread > 0.01:
            warnings.append(f"Trials disagree by {spread * 100:.1f} %: check for slip or stalls.")
    new = old_steps_per_mm / ratio
    lo, hi = 40.0, 160.0   # firmware accepts 0.5x-2x the nominal 80 steps/mm
    if not lo <= new <= hi:
        warnings.append(f"{new:.3f} steps/mm is outside what the firmware accepts ({lo:.0f}-{hi:.0f}).")
    return TravelResult(old_steps_per_mm, new, ratio, warnings)


def apply_travel(machine, new_steps_per_mm: float) -> None:
    machine.set("steps_per_mm", new_steps_per_mm)
    machine.save()


# -------------------------------------------------------------------- records --

def _jsonable(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    raise TypeError(f"not JSON serialisable: {type(obj)}")


def save_record(directory: Path, kind: str, data: dict) -> Path:
    """Write Outputs/calibration/<timestamp>_<kind>.json and return its path."""
    now = datetime.now()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%Y-%m-%d_%H%M%S}_{kind}.json"
    record = {"kind": kind, "created": now.isoformat(timespec="seconds"), **data}
    path.write_text(json.dumps(record, indent=2, default=_jsonable))
    return path


def latest_record(directory: Path, kind: str) -> tuple[Path, dict] | None:
    if not directory.is_dir():
        return None
    paths = sorted(directory.glob(f"*_{kind}.json"))
    for path in reversed(paths):
        try:
            return path, json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return None
