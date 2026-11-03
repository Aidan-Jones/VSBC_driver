"""Writes one folder per test:

    Outputs/tests/<date>/<time>_<specimen>/
        data.csv      every sample, written while the test runs
        meta.json     test spec, firmware, settings, calibration, times, stop reason
        summary.json  analysis results (summary.txt: the same, readable)
        plot.png      load vs extension (and stress vs strain with geometry)
        serial.log    serial traffic during the test, without the data lines

data.csv, summary.json and the analysis stay in newtons and millimetres. With
other display units, data.csv gets extra columns in them, and summary.txt and
plot.png use them.
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

from vsbc.analysis import REVERSED_TENSION_NOTE, Summary, analyze, tension_reads_negative, tension_threshold_n
from vsbc.plotting import save_test_plot
from vsbc.protocol import Sample
from vsbc.units import Units

CSV_COLUMNS = ["time_s", "position_mm", "extension_mm", "load_N", "stress_MPa", "strain", "raw_counts", "state"]


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_.") or "specimen"


def _cell(value: float, digits: int) -> str:
    return "" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def unit_columns(units: Units) -> list[str]:
    """data.csv columns in the display units, added after the N and mm ones (none for N and mm)."""
    columns = []
    if units.length != "mm":
        columns += [f"position_{units.length}", f"extension_{units.length}"]
    if units.force != "N":
        columns.append(f"load_{units.force}")
    if units.stress_label != "MPa":
        columns.append(f"stress_{units.stress_label}")
    return columns


def unit_cells(units: Units, pos_mm: float, ext_mm: float, load_n: float, stress_mpa: float) -> list[str]:
    cells = []
    if units.length != "mm":
        digits = units.length_digits
        cells += [_cell(units.length_from_mm(pos_mm), digits), _cell(units.length_from_mm(ext_mm), digits)]
    if units.force != "N":
        cells.append(_cell(units.force_from_n(load_n), units.force_digits + 1))
    if units.stress_label != "MPa":
        cells.append(_cell(units.stress_from_mpa(stress_mpa), 2))
    return cells


class TestRecorder:
    __test__ = False   # not a pytest test class

    def __init__(self, tests_dir: Path, specimen_id: str, meta: dict,
                 area_mm2: float | None = None, gauge_length_mm: float | None = None,
                 now: datetime | None = None, units: Units | None = None, capacity_n: float | None = None):
        now = now or datetime.now()
        base = Path(tests_dir) / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}_{safe_name(specimen_id)}"
        folder, n = base, 2
        while folder.exists():
            folder = base.with_name(f"{base.name}_{n}")
            n += 1
        folder.mkdir(parents=True)
        self.folder = folder
        self.specimen_id = specimen_id
        self.area = area_mm2 or None
        self.gauge = gauge_length_mm or None
        self.units = units or Units()
        self.capacity_n = capacity_n
        self.columns = CSV_COLUMNS + unit_columns(self.units)
        self.warnings: list[str] = []
        self.meta = {**meta, "units": self.units.to_dict(), "started": now.isoformat(timespec="seconds"),
                     "status": "running"}
        self._write_json("meta.json", self.meta)

        self._csv_file = open(folder / "data.csv", "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(self.columns)
        self._serial = open(folder / "serial.log", "w", encoding="utf-8")
        self._last_flush = time.monotonic()
        self.t0_ms: float | None = None
        self.start_pos: float | None = None
        self.time: list[float] = []
        self.ext: list[float] = []
        self.load: list[float] = []
        self.stress: list[float] = []
        self.strain: list[float] = []
        self.closed = False

    def set_start_position(self, pos_mm: float) -> None:
        self.start_pos = pos_mm

    def add(self, s: Sample) -> None:
        if self.closed:
            return
        if self.t0_ms is None:
            self.t0_ms = s.t_ms
        if self.start_pos is None:
            self.start_pos = s.pos_mm
        t = (s.t_ms - self.t0_ms) / 1000.0
        ext = s.pos_mm - self.start_pos
        stress = s.load_n / self.area if self.area else math.nan
        strain = ext / self.gauge if self.gauge else math.nan
        row = [
            f"{t:.4f}", f"{s.pos_mm:.4f}", f"{ext:.4f}", _cell(s.load_n, 3),
            _cell(stress, 4), _cell(strain, 6), s.raw, s.state,
        ]
        if not self.units.is_default:
            row += unit_cells(self.units, s.pos_mm, ext, s.load_n, stress)
        self._csv.writerow(row)
        self.time.append(t)
        self.ext.append(ext)
        self.load.append(s.load_n)
        self.stress.append(stress)
        self.strain.append(strain)
        now = time.monotonic()
        if now - self._last_flush >= 1.0:
            self._csv_file.flush()
            self._last_flush = now

    def log_serial(self, text: str, sent: bool) -> None:
        """Raw-line listener: everything except data lines."""
        if self.closed or text.startswith("D "):
            return
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._serial.write(f"[{stamp}] {'> ' if sent else ''}{text}\n")

    def finish(self, stop_reason: str | None, *, move_end: dict | None = None, aborted: bool = False,
               error: str | None = None, break_drop_pct: float = 40.0, break_min_n: float = 0.0,
               warnings: list[str] | None = None) -> Summary:
        self.closed = True
        self._csv_file.close()
        self._serial.close()

        summary = analyze(
            self.time, self.ext, self.load,
            area_mm2=self.area, gauge_length_mm=self.gauge,
            break_drop_pct=break_drop_pct, break_min_n=break_min_n, stop_reason=stop_reason,
        )
        self.warnings = list(warnings or [])
        if tension_reads_negative(self.load, self.ext, tension_threshold_n(self.capacity_n)):
            self.warnings.append(REVERSED_TENSION_NOTE)
        self._write_json("summary.json", summary.to_dict())
        text = f"Specimen: {self.specimen_id}\nStarted:  {self.meta['started']}\n\n{summary.to_text(self.units)}\n"
        if not self.units.is_default:
            text += f"\nUnits: {self.units.force}, {self.units.length} (summary.json is in N and mm)\n"
        if self.warnings:
            text += "\n" + "\n".join(f"Warning: {w}" for w in self.warnings) + "\n"
        (self.folder / "summary.txt").write_text(text, encoding="utf-8")
        if self.time:
            save_test_plot(
                self.folder / "plot.png", self.ext, self.load, summary,
                title=f"{self.specimen_id}  ({self.meta['started']})",
                stress_mpa=self.stress if self.area and self.gauge else None,
                strain=self.strain if self.area and self.gauge else None,
                units=self.units,
            )
        self.meta.update({
            "ended": datetime.now().isoformat(timespec="seconds"),
            "status": "aborted" if aborted else "complete",
            "stop_reason": stop_reason,
            "move_end": move_end,
            "error": error,
            "warnings": self.warnings,
            "samples": len(self.time),
            # The Mega's clock at time_s = 0 and the crosshead there: the camera sync needs both.
            "mega_t0_ms": self.t0_ms,
            "start_position_mm": self.start_pos,
        })
        self._write_json("meta.json", self.meta)
        return summary

    def update_meta(self, **fields) -> None:
        """Add to meta.json after the test (the camera summary)."""
        self.meta.update(fields)
        self._write_json("meta.json", self.meta)

    def _write_json(self, name: str, data: dict) -> None:
        def default(obj):
            if isinstance(obj, Path):
                return str(obj)
            if isinstance(obj, float) and not math.isfinite(obj):
                return None
            return str(obj)

        (self.folder / name).write_text(json.dumps(data, indent=2, default=default), encoding="utf-8")
