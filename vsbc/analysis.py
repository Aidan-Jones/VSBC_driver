"""Results of a pull-to-failure test from extension and load arrays.

Extension comes from the crosshead (motor steps), so it includes the frame and
grip compliance: stiffness and modulus are *apparent* values.
"""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from vsbc.units import Units

_trapezoid = getattr(np, "trapezoid", None) or np.trapz


@dataclass
class Summary:
    n_samples: int
    duration_s: float
    max_extension_mm: float
    peak_load_n: float
    extension_at_peak_mm: float
    break_detected: bool
    break_load_n: float | None
    break_extension_mm: float | None
    stiffness_n_per_mm: float | None
    stiffness_r2: float | None
    fit_range_n: tuple[float, float] | None
    energy_to_peak_j: float
    energy_to_end_j: float
    stop_reason: str | None = None
    # Only with specimen geometry:
    area_mm2: float | None = None
    gauge_length_mm: float | None = None
    uts_mpa: float | None = None
    modulus_mpa: float | None = None
    strain_at_peak: float | None = None
    elongation_at_break_pct: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in d.items()}

    def to_text(self, units: Units | None = None) -> str:
        """Readable summary, in newtons and millimetres unless other display units are given."""
        u = units or Units()

        def f(value, fmt, unit="", convert=None):
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                return "-"
            return f"{convert(value) if convert else value:{fmt}}{unit}"

        fd = f".{u.force_digits}f"
        ld = f".{u.length_digits - 1}f"
        force, length = (f" {u.force}", u.force_from_n), (f" {u.length}", u.length_from_mm)
        lines = [
            f"Stop reason:           {self.stop_reason or '-'}",
            f"Samples:               {self.n_samples} over {self.duration_s:.1f} s",
            f"Peak load:             {f(self.peak_load_n, fd, *force)} at {f(self.extension_at_peak_mm, ld, *length)}",
            f"Break:                 "
            + (f"{f(self.break_load_n, fd, *force)} at {f(self.break_extension_mm, ld, *length)}"
               if self.break_detected else "not detected"),
            f"Max extension:         {f(self.max_extension_mm, ld, *length)}",
            f"Apparent stiffness:    {f(self.stiffness_n_per_mm, '.1f', ' ' + u.stiffness_label, u.stiffness_from_n_per_mm)}"
            + (f" (R2 {self.stiffness_r2:.4f}, fit {u.force_from_n(self.fit_range_n[0]):.1f}-"
               f"{u.force_from_n(self.fit_range_n[1]):.1f} {u.force})"
               if self.stiffness_n_per_mm is not None else ""),
            f"Energy to peak / end:  {f(self.energy_to_peak_j, '.3f', ' ' + u.energy_label, u.energy_from_j)} / "
            f"{f(self.energy_to_end_j, '.3f', ' ' + u.energy_label, u.energy_from_j)}",
        ]
        stress_fmt = ".0f" if u.stress_label == "psi" else ".2f"
        if self.area_mm2:
            area = "mm2" if u.length == "mm" else f"{u.length}2"
            lines += [
                f"Area:                  {u.area_from_mm2(self.area_mm2):g} {area}",
                f"UTS:                   {f(self.uts_mpa, stress_fmt, ' ' + u.stress_label, u.stress_from_mpa)}",
            ]
        if self.area_mm2 and self.gauge_length_mm:
            lines += [
                f"Gauge length:          {u.length_from_mm(self.gauge_length_mm):g} {u.length}",
                f"Apparent modulus:      {f(self.modulus_mpa, '.0f', ' ' + u.stress_label, u.stress_from_mpa)}",
                f"Strain at peak:        {f(self.strain_at_peak, '.4f')}",
                f"Elongation at break:   {f(self.elongation_at_break_pct, '.2f', ' %')}",
            ]
        return "\n".join(lines)


def find_break(load: np.ndarray, drop_pct: float, min_peak_n: float, confirm: int = 2) -> int | None:
    """Index of the last sample before the load fell drop_pct % below its running peak.

    Like the firmware, the drop must last `confirm` samples in a row. The peak must
    reach min_peak_n and a tenth of the test's peak, so a peak in the tare noise
    (min_peak_n = 0) does not arm it.
    """
    load = np.asarray(load, dtype=float)
    finite = load[np.isfinite(load)]
    if drop_pct <= 0 or finite.size == 0:
        return None
    floor = max(min_peak_n, 0.1 * float(finite.max()))
    peak = -math.inf
    first_below: int | None = None
    below = 0
    for i, value in enumerate(load):
        if not math.isfinite(value):
            continue
        if value > peak:
            peak = value
            below = 0
        elif peak >= floor and peak > 0 and value < peak * (1 - drop_pct / 100.0):
            if below == 0:
                first_below = i
            below += 1
            if below >= confirm:
                return max(first_below - 1, 0)
        else:
            below = 0
    return None


REVERSED_TENSION_NOTE = (
    "Tension read as negative load while pulling: the load-cell calibration's sign is reversed "
    "(Calibrate tab > Flip load sign). Break detection could not arm, and the peak load is not valid."
)


def tension_threshold_n(capacity_n: float | None) -> float:
    """How far below zero the load must go before it counts as reversed: 2 N or 1 % of capacity."""
    return max(2.0, 0.01 * capacity_n) if capacity_n else 2.0


def tension_reads_negative(load_n, ext_mm, threshold_n: float = 2.0, run: int = 5) -> bool:
    """True when pulling made the load go negative, i.e. the calibration's sign is reversed.

    The load must stay below -threshold_n for `run` samples in a row while the
    extension grows, and never rise above +threshold_n (a real tension test
    that starts with a little compression is fine).
    """
    f = np.asarray(load_n, dtype=float)
    e = np.asarray(ext_mm, dtype=float)
    ok = np.isfinite(f) & np.isfinite(e)
    f, e = f[ok], e[ok]
    if f.size < run or f.max() > threshold_n:
        return False
    count = 0
    for i in range(f.size):
        rising = i == 0 or e[i] >= e[i - 1] - 1e-9
        count = count + 1 if f[i] < -threshold_n and rising else 0
        if count >= run:
            return True
    return False


def fit_stiffness(ext: np.ndarray, load: np.ndarray, peak_index: int, lo_frac: float, hi_frac: float):
    """Linear fit of load vs extension between lo_frac and hi_frac of the peak, before the peak."""
    if peak_index < 2:
        return None
    peak = load[peak_index]
    e, f = ext[: peak_index + 1], load[: peak_index + 1]
    mask = np.isfinite(f) & (f >= lo_frac * peak) & (f <= hi_frac * peak)
    if mask.sum() < 3 or np.ptp(e[mask]) == 0:
        return None
    k, b = np.polyfit(e[mask], f[mask], 1)
    pred = k * e[mask] + b
    ss_res = float(np.sum((f[mask] - pred) ** 2))
    ss_tot = float(np.sum((f[mask] - f[mask].mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return float(k), float(b), r2, (float(lo_frac * peak), float(hi_frac * peak))


def analyze(
    time_s,
    ext_mm,
    load_n,
    *,
    area_mm2: float | None = None,
    gauge_length_mm: float | None = None,
    break_drop_pct: float = 40.0,
    break_min_n: float = 0.0,
    fit_lo_frac: float = 0.10,
    fit_hi_frac: float = 0.40,
    stop_reason: str | None = None,
) -> Summary:
    t = np.asarray(time_s, dtype=float)
    e = np.asarray(ext_mm, dtype=float)
    f = np.asarray(load_n, dtype=float)
    finite = np.isfinite(f) & np.isfinite(e)
    if not finite.any():
        return Summary(len(f), float(t[-1] - t[0]) if len(t) > 1 else 0.0, float("nan"), float("nan"),
                       float("nan"), False, None, None, None, None, None, 0.0, 0.0, stop_reason,
                       area_mm2, gauge_length_mm)
    t, e, f = t[finite], e[finite], f[finite]

    peak_i = int(np.argmax(f))
    brk = find_break(f, break_drop_pct, break_min_n)
    end_i = brk if brk is not None else len(f) - 1
    fit = fit_stiffness(e, f, peak_i, fit_lo_frac, fit_hi_frac)

    s = Summary(
        n_samples=len(f),
        duration_s=float(t[-1] - t[0]) if len(t) > 1 else 0.0,
        max_extension_mm=float(np.max(e)),
        peak_load_n=float(f[peak_i]),
        extension_at_peak_mm=float(e[peak_i]),
        break_detected=brk is not None,
        break_load_n=float(f[brk]) if brk is not None else None,
        break_extension_mm=float(e[brk]) if brk is not None else None,
        stiffness_n_per_mm=fit[0] if fit else None,
        stiffness_r2=fit[2] if fit else None,
        fit_range_n=fit[3] if fit else None,
        energy_to_peak_j=float(_trapezoid(f[: peak_i + 1], e[: peak_i + 1])) / 1000.0,
        energy_to_end_j=float(_trapezoid(f[: end_i + 1], e[: end_i + 1])) / 1000.0,
        stop_reason=stop_reason,
        area_mm2=area_mm2 or None,
        gauge_length_mm=gauge_length_mm or None,
    )
    if area_mm2:
        s.uts_mpa = s.peak_load_n / area_mm2
    if gauge_length_mm:
        s.strain_at_peak = s.extension_at_peak_mm / gauge_length_mm
        if brk is not None:
            s.elongation_at_break_pct = 100.0 * s.break_extension_mm / gauge_length_mm
    if area_mm2 and gauge_length_mm and s.stiffness_n_per_mm is not None:
        s.modulus_mpa = s.stiffness_n_per_mm * gauge_length_mm / area_mm2
    return s


def load_csv(path: Path) -> dict[str, np.ndarray]:
    """Read a test data.csv into column arrays (empty cells become nan)."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns: dict[str, list[float]] = {name: [] for name in reader.fieldnames or []}
        for row in reader:
            for name in columns:
                cell = row.get(name, "")
                try:
                    columns[name].append(float(cell) if cell not in ("", None) else math.nan)
                except ValueError:
                    columns[name].append(math.nan)
    return {name: np.array(values) for name, values in columns.items()}
