"""Static report plots (PNG), drawn with matplotlib's Agg canvas.

Uses the object API only, so it never changes the global matplotlib backend
and is safe to call from a worker thread while the Qt GUI is running.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from vsbc.analysis import Summary


def save_test_plot(path: Path, ext_mm, load_n, summary: Summary, title: str = "",
                   stress_mpa=None, strain=None) -> Path:
    ext = np.asarray(ext_mm, dtype=float)
    load = np.asarray(load_n, dtype=float)
    with_stress = stress_mpa is not None and strain is not None and np.isfinite(np.asarray(stress_mpa, dtype=float)).any()

    fig = Figure(figsize=(11 if with_stress else 7, 4.8), dpi=120)
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2 if with_stress else 1, squeeze=False)[0]

    ax = axes[0]
    ax.plot(ext, load, lw=1.2, color="#1f5fa8")
    ax.set_xlabel("Extension (mm)")
    ax.set_ylabel("Load (N)")
    ax.grid(True, alpha=0.3)
    if np.isfinite(summary.peak_load_n):
        ax.plot([summary.extension_at_peak_mm], [summary.peak_load_n], "o", color="#c0392b",
                label=f"Peak {summary.peak_load_n:.1f} N")
    if summary.break_detected:
        ax.plot([summary.break_extension_mm], [summary.break_load_n], "x", ms=9, mew=2, color="#333333",
                label=f"Break at {summary.break_extension_mm:.3f} mm")
    if summary.stiffness_n_per_mm is not None and summary.fit_range_n is not None:
        lo, hi = summary.fit_range_n
        mask = np.isfinite(load) & (load >= lo) & (load <= hi)
        if mask.any():
            e0 = ext[mask]
            b = float(np.mean(load[mask] - summary.stiffness_n_per_mm * e0))
            xs = np.array([max(e0.min() - 0.2 * np.ptp(e0), 0), e0.max() + 0.5 * np.ptp(e0)])
            ax.plot(xs, summary.stiffness_n_per_mm * xs + b, "--", lw=1, color="#27ae60",
                    label=f"Stiffness {summary.stiffness_n_per_mm:.0f} N/mm")
    ax.legend(loc="best", fontsize=8)
    ax.set_title("Load vs extension")

    if with_stress:
        ax2 = axes[1]
        ax2.plot(np.asarray(strain, dtype=float), np.asarray(stress_mpa, dtype=float), lw=1.2, color="#8e44ad")
        ax2.set_xlabel("Strain (crosshead, mm/mm)")
        ax2.set_ylabel("Stress (MPa)")
        ax2.grid(True, alpha=0.3)
        ax2.set_title("Engineering stress vs strain")

    if title:
        fig.suptitle(title)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    return path
