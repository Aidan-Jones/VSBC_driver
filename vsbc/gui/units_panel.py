"""Units tab: the force and travel units the GUI shows and accepts.

The machine, data.csv's N / mm columns and summary.json stay in newtons and
millimetres; with other units data.csv gets extra columns in them, and
summary.txt, plot.png and the video use them. The choice is kept in
Outputs/preferences.json (config.toml [units] is the default).
"""

from __future__ import annotations

from vsbc.gui.common import QtWidgets, button
from vsbc.gui.panels import Panel
from vsbc.units import FORCE_UNITS, LENGTH_UNITS, Units

FORCE_NAMES = {"N": "N (newton)", "lbf": "lbf (pound-force)"}
LENGTH_NAMES = {"mm": "mm", "cm": "cm", "m": "m", "in": "in (inch)"}


class UnitsPanel(Panel):
    def __init__(self, win):
        super().__init__(win)
        self.force_group = QtWidgets.QButtonGroup(self)
        self.length_group = QtWidgets.QButtonGroup(self)
        force_box = QtWidgets.QGroupBox("Force (load)")
        force_row = QtWidgets.QVBoxLayout(force_box)
        for i, unit in enumerate(FORCE_UNITS):
            b = QtWidgets.QRadioButton(FORCE_NAMES[unit])
            b.setMinimumHeight(32)
            self.force_group.addButton(b, i)
            force_row.addWidget(b)
        length_box = QtWidgets.QGroupBox("Travel (position, extension, rates, area)")
        length_row = QtWidgets.QVBoxLayout(length_box)
        for i, unit in enumerate(LENGTH_UNITS):
            b = QtWidgets.QRadioButton(LENGTH_NAMES[unit])
            b.setMinimumHeight(32)
            self.length_group.addButton(b, i)
            length_row.addWidget(b)
        self.example = QtWidgets.QLabel()
        self.example.setWordWrap(True)
        note = QtWidgets.QLabel(
            "Readouts, plots, the test and jog settings, the calibration screens and the results use these units. "
            "data.csv keeps its N and mm columns (the analysis and the camera sync read them) and gets extra "
            "columns in these units; summary.txt, plot.png and the camera video use them too. The machine's "
            "calibration (steps/mm, counts/N) does not change.")
        note.setWordWrap(True)
        self.reset_btn = button("Reset to the config.toml defaults", self.reset)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(force_box)
        layout.addWidget(length_box)
        layout.addWidget(self.example)
        layout.addWidget(note)
        layout.addWidget(self.reset_btn)
        layout.addStretch(1)
        self.apply_units(win.units)
        self.force_group.idClicked.connect(self._changed)
        self.length_group.idClicked.connect(self._changed)

    def _changed(self, _id: int) -> None:
        units = Units(FORCE_UNITS[self.force_group.checkedId()], LENGTH_UNITS[self.length_group.checkedId()])
        if units != self.win.units:
            self.win.set_units(units)

    def reset(self) -> None:
        self.win.set_units(Units(self.win.config.units.force, self.win.config.units.length), remember=False)

    def apply_units(self, units: Units) -> None:
        self.force_group.button(FORCE_UNITS.index(units.force)).setChecked(True)
        self.length_group.button(LENGTH_UNITS.index(units.length)).setChecked(True)
        self.example.setText(
            f"100 N = {units.format_force(100.0)}   ·   10 mm = {units.format_length(10.0)}   ·   "
            f"5 mm/min = {units.length_from_mm(5.0):.4g} {units.rate_min_label}   ·   "
            f"400 N/mm = {units.stiffness_from_n_per_mm(400.0):.4g} {units.stiffness_label}")
