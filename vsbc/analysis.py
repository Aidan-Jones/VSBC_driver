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

    def to_text(self) -> str:
        def f(value, fmt, unit=""):
            return "-" if value is None or (isinstance(value, float) and not math.isfinite(value)) else f"{value:{fmt}}{unit}"

        lines = [
            f"Stop reason:           {self.stop_reason or '-'}",
            f"Samples:               {self.n_samples} over {self.duration_s:.1f} s",
            f"Peak load:             {f(self.peak_load_n, '.2f', ' N')} at {f(self.extension_at_peak_mm, '.3f', ' mm')}",
            f"Break:                 "
            + (f"{f(self.break_load_n, '.2f', ' N')} at {f(self.break_extension_mm, '.3f', ' mm')}"
               if self.break_detected else "not detected"),
            f"Max extension:         {f(self.max_extension_mm, '.3f', ' mm')}",
            f"Apparent stiffness:    {f(self.stiffness_n_per_mm, '.1f', ' N/mm')}"
            + (f" (R2 {self.stiffness_r2:.4f}, fit {self.fit_range_n[0]:.1f}-{self.fit_range_n[1]:.1f} N)"
               if self.stiffness_n_per_mm is not None else ""),
            f"Energy to peak / end:  {f(self.energy_to_peak_j, '.3f', ' J')} / {f(self.energy_to_end_j, '.3f', ' J')}",
        ]
        if self.area_mm2:
            lines += [
                f"Area:                  {self.area_mm2:g} mm2",
                f"UTS:                   {f(self.uts_mpa, '.2f', ' MPa')}",
            ]
        if self.area_mm2 and self.gauge_length_mm:
            lines += [
                f"Gauge length:          {self.gauge_length_mm:g} mm",
                f"Apparent modulus:      {f(self.modulus_mpa, '.0f', ' MPa')}",
                f"Strain at peak:        {f(self.strain_at_peak, '.4f')}",
                f"Elongation at break:   {f(self.elongation_at_break_pct, '.2f', ' %')}",
            ]
        return "\n".join(lines)


def find_break(load: np.ndarray, drop_pct: float, min_peak_n: float) -> int | None:
    """Index of the last sample before the load fell drop_pct % below its running peak."""
    if drop_pct <= 0 or len(load) == 0:
        return None
    peak = -math.inf
    for i, value in enumerate(load):
        if not math.isfinite(value):
            continue
        if value > peak:
            peak = value
        elif peak >= min_peak_n and peak > 0 and value < peak * (1 - drop_pct / 100.0):
            return max(i - 1, 0)
    return None


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
