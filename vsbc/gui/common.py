"""Qt imports (through pyqtgraph, so PyQt5, PyQt6 or PySide6 all work) and small helpers."""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from vsbc.units import MM_PER, N_PER, Units

Qt = QtCore.Qt
QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut

STOP_STYLE = (
    "QPushButton { background: #c0392b; color: white; font-weight: bold; font-size: 20pt;"
    " border-radius: 8px; padding: 8px 28px; }"
    "QPushButton:pressed { background: #922b21; }"
)
START_STYLE = (
    "QPushButton { background: #1e8449; color: white; font-weight: bold; border-radius: 6px; padding: 8px; }"
    "QPushButton:disabled { background: #a9cbb7; }"
)
READOUT_STYLE = "QLabel { font-size: 22pt; font-weight: bold; font-family: Consolas, 'DejaVu Sans Mono', monospace; }"
CAPTION_STYLE = "QLabel { color: #555; font-size: 9pt; }"
BANNER_STYLE = "QLabel { background: #fdebd0; color: #7e5109; padding: 4px 8px; border-radius: 4px; }"
BANNER_CRITICAL_STYLE = "QLabel { background: #f5b7b1; color: #78281f; padding: 4px 8px; border-radius: 4px; }"
HIGH_SPEED_STYLE = (
    "QPushButton { background: #7d3c98; color: white; font-weight: bold; border-radius: 6px; padding: 6px 14px; }"
    "QPushButton:checked { background: #e67e22; }"
    "QPushButton:disabled { background: #c9b3d3; }"
)


def apply_style(app, font_pt: int) -> None:
    font = app.font()
    font.setPointSize(font_pt)
    app.setFont(font)
    app.setStyleSheet(
        "QPushButton { min-height: 34px; padding: 4px 10px; }"
        "QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox { min-height: 30px; }"
        "QTabBar::tab { min-height: 32px; padding: 4px 14px; }"
    )


def fmt(value: float | None, digits: int, unit: str = "") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}{(' ' + unit) if unit else ''}"


def spin(lo: float, hi: float, value: float, decimals: int = 2, step: float | None = None,
         suffix: str = "") -> QtWidgets.QDoubleSpinBox:
    box = QtWidgets.QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setDecimals(decimals)
    box.setValue(value)
    if step is not None:
        box.setSingleStep(step)
    if suffix:
        box.setSuffix(f" {suffix}")
    box.setKeyboardTracking(False)
    return box


def nice_step(x: float) -> float:
    """1, 2 or 5 times a power of ten, at or below x."""
    if not x > 0:
        return 1.0
    p = 10 ** math.floor(math.log10(x))
    return next(m * p for m in (5, 2, 1) if m * p <= x * 1.0001)


class QuantitySpin(QtWidgets.QDoubleSpinBox):
    """Spin box holding an SI value (N, mm, mm/min, mm/s, mm²) shown in the display units.

    `si_value()` is what the machine gets. It only changes when the operator
    edits the box, so switching the units back and forth never drifts it.
    """

    def __init__(self, kind: str, lo_si: float, hi_si: float, value_si: float, units: Units,
                 decimals: int = 2, step_si: float | None = None, special: str = ""):
        super().__init__()
        if kind not in ("force", "length", "rate_min", "rate_s", "area"):
            raise ValueError(f"unknown quantity {kind!r}")
        self.kind = kind
        self._lo, self._hi, self._si = lo_si, hi_si, value_si
        self._decimals, self._step_si = decimals, step_si
        self._updating = False
        self._units = units
        if special:
            self.setSpecialValueText(special)
        self.setKeyboardTracking(False)
        self.valueChanged.connect(self._edited)
        self.apply_units(units)

    def _factor(self, units: Units) -> float:
        """SI per display unit."""
        if self.kind == "force":
            return N_PER[units.force]
        if self.kind == "area":
            return MM_PER[units.length] ** 2
        return MM_PER[units.length]

    def _suffix(self, units: Units) -> str:
        return {"force": units.force, "length": units.length, "rate_min": units.rate_min_label,
                "rate_s": units.rate_s_label, "area": units.area_label}[self.kind]

    def apply_units(self, units: Units) -> None:
        self._units = units
        f = self._factor(units)
        self._updating = True
        try:
            self.setDecimals(self._decimals + (math.ceil(math.log10(f) - 1e-9) if f > 1 else 0))
            self.setRange(self._lo / f, self._hi / f)
            if self._step_si:
                self.setSingleStep(nice_step(self._step_si / f))
            self.setSuffix(f" {self._suffix(units)}")
            self.setValue(self._si / f)
        finally:
            self._updating = False

    def _edited(self, value: float) -> None:
        if not self._updating:
            self._si = value * self._factor(self._units)

    def si_value(self) -> float:
        return self._si

    def set_si_value(self, value: float) -> None:
        self._si = value
        self._updating = True
        try:
            self.setValue(value / self._factor(self._units))
        finally:
            self._updating = False


def button(text: str, slot=None, style: str | None = None, min_height: int | None = None) -> QtWidgets.QPushButton:
    b = QtWidgets.QPushButton(text)
    if slot is not None:
        b.clicked.connect(slot)
    if style:
        b.setStyleSheet(style)
    if min_height:
        b.setMinimumHeight(min_height)
    return b


class GrowingArray:
    """Append-only float buffer that hands pyqtgraph a view without copying."""

    def __init__(self, capacity: int = 4096):
        self._data = np.empty(capacity, dtype=float)
        self._n = 0

    def append(self, value: float) -> None:
        if self._n == len(self._data):
            self._data = np.concatenate([self._data, np.empty(len(self._data), dtype=float)])
        self._data[self._n] = value
        self._n += 1

    def clear(self) -> None:
        self._n = 0

    def view(self) -> np.ndarray:
        return self._data[: self._n]

    def __len__(self) -> int:
        return self._n


def exec_app(app) -> int:
    run = getattr(app, "exec", None) or app.exec_
    return run()


__all__ = ["QtCore", "QtGui", "QtWidgets", "Qt", "QShortcut", "pg", "np", "fmt", "spin", "button",
           "GrowingArray", "QuantitySpin", "nice_step", "apply_style", "exec_app", "STOP_STYLE", "START_STYLE",
           "READOUT_STYLE", "CAPTION_STYLE", "BANNER_STYLE", "BANNER_CRITICAL_STYLE", "HIGH_SPEED_STYLE"]
