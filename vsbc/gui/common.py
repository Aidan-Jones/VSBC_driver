"""Qt imports (through pyqtgraph, so PyQt5, PyQt6 or PySide6 all work) and small helpers."""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

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
           "GrowingArray", "apply_style", "exec_app", "STOP_STYLE", "START_STYLE", "READOUT_STYLE",
           "CAPTION_STYLE", "BANNER_STYLE"]
