"""Calibration dialogs: load cell (multi-point) and travel (steps/mm).

With the simulator connected, the dialogs also "hang" the entered load on the
simulated load cell and fill in the travel a dial indicator would show, so the
whole flow can be tried without hardware.
"""

from __future__ import annotations

import time
from dataclasses import asdict

from vsbc.calibration import (
    LoadFit, LoadPoint, TravelTrial, apply_load_fit, apply_travel, collect_raw, fit_load_calibration,
    mass_to_newtons, save_record, travel_correction,
)
from vsbc.gui.common import QtWidgets, button, spin

AVERAGE_S = 3.0


class _Dialog(QtWidgets.QDialog):
    def __init__(self, win, title: str):
        super().__init__(win)
        self.win = win
        self.machine = win.machine
        self.setWindowTitle(title)
        self.setMinimumWidth(600)
        self._busy = False

    def open_dialog(self) -> None:
        run = getattr(self, "exec", None) or self.exec_
        run()

    def _failed(self, exc: BaseException) -> None:
        self._set_busy(False)
        self.win.warn(self.windowTitle(), str(exc))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        pass


def _intro(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    return label


class LoadCalibrationDialog(_Dialog):
    UNITS = ("g", "kg", "N", "lb")

    def __init__(self, win):
        super().__init__(win, "Load cell calibration")
        self.points: list[LoadPoint] = []
        self.fit: LoadFit | None = None
        self.idle_noise: float | None = None

        self.tare_btn = button("1.  Tare + record zero (no load)", self.tare_and_zero)
        self.value = spin(0.0, 1e6, 1000.0, 3, 100.0)
        self.unit = QtWidgets.QComboBox()
        self.unit.addItems(self.UNITS)
        self.record_btn = button("2.  Record this load", self.record)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Load (N)", "Mean (counts)", "Std (counts)", "Samples"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.remove_btn = button("Remove last point", self.remove_last)
        self.result = QtWidgets.QLabel("Record the zero point and at least one known load.")
        self.result.setWordWrap(True)
        self.save_btn = button("3.  Save to machine", self.save)
        close_btn = button("Close", self.reject)

        load_row = QtWidgets.QHBoxLayout()
        load_row.addWidget(QtWidgets.QLabel("Known load"))
        load_row.addWidget(self.value, 1)
        load_row.addWidget(self.unit)
        load_row.addWidget(self.record_btn)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.save_btn, 1)
        bottom.addWidget(close_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(_intro(
            "Apply the known loads in the tension direction, for example by hanging weights from the "
            "load cell. Use 2-5 loads that span the range you will test in, and let each one settle before "
            f"recording it (each point is averaged for {AVERAGE_S:g} s)."))
        layout.addWidget(self.tare_btn)
        layout.addLayout(load_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.remove_btn)
        layout.addWidget(self.result)
        layout.addLayout(bottom)
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self._busy
        has_zero = any(p.force_n == 0 for p in self.points)
        self.tare_btn.setEnabled(idle)
        self.record_btn.setEnabled(idle and has_zero)
        self.remove_btn.setEnabled(idle and bool(self.points))
        self.save_btn.setEnabled(idle and self.fit is not None)

    def tare_and_zero(self) -> None:
        sim = self.machine.sim

        def work():
            if sim:
                sim.set_external_load(0.0)
            self.machine.tare()
            return collect_raw(self.machine, AVERAGE_S)

        self._set_busy(True)
        self.result.setText("Taring, then averaging the zero reading...")
        self.win.run_async(work, self._zero_done, self._failed)

    def _zero_done(self, res) -> None:
        mean, std, n = res
        self.idle_noise = std
        self.points = [LoadPoint(0.0, mean, std, n, "zero")] + [p for p in self.points if p.force_n != 0]
        self._set_busy(False)
        self._refresh()

    def record(self) -> None:
        amount, unit = self.value.value(), self.unit.currentText()
        force = mass_to_newtons(amount, unit, self.win.config.loadcell.gravity)
        if force == 0:
            self.win.warn(self.windowTitle(), "Enter a non-zero load.")
            return
        label = f"{amount:g} {unit}"
        sim = self.machine.sim

        def work():
            if sim:
                sim.set_external_load(force)
                time.sleep(0.2)
            return collect_raw(self.machine, AVERAGE_S)

        self._set_busy(True)
        self.result.setText(f"Averaging {label} ({force:.3f} N)...")
        self.win.run_async(work, lambda res: self._point_done(force, label, res), self._failed)

    def _point_done(self, force: float, label: str, res) -> None:
        mean, std, n = res
        self.points.append(LoadPoint(force, mean, std, n, label))
        self._set_busy(False)
        self._refresh()
        if self.idle_noise and std > 3 * self.idle_noise:
            self.win.warn(self.windowTitle(),
                          f"The {label} reading was noisy (std {std:.0f} counts, {self.idle_noise:.0f} at zero). "
                          "If the load was still swinging, remove the point and record it again.")

    def remove_last(self) -> None:
        if self.points:
            self.points.pop()
            self._refresh()

    def _refresh(self) -> None:
        self.table.setRowCount(len(self.points))
        for row, p in enumerate(self.points):
            for col, text in enumerate((f"{p.force_n:.3f}", f"{p.raw_mean:.0f}", f"{p.raw_std:.1f}", str(p.n))):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem(text))
        self.fit = None
        if len(self.points) >= 2:
            try:
                self.fit = fit_load_calibration(self.points, self.win.config.loadcell.capacity_n)
            except ValueError as e:
                self.result.setText(str(e))
        if self.fit:
            f = self.fit
            pct = f" ({f.max_residual_pct_fs:.3f} % of capacity)" if f.max_residual_pct_fs is not None else ""
            self.result.setText(
                f"Scale {f.scale:.4f} counts/N, zero {f.offset:.0f} counts, R² = {f.r2:.6f}\n"
                f"Largest error {f.max_residual_n:.3f} N{pct}")
        elif len(self.points) < 2:
            self.result.setText("Record the zero point and at least one known load.")
        self._update_buttons()

    def save(self) -> None:
        fit, capacity = self.fit, self.win.config.loadcell.capacity_n

        def work():
            apply_load_fit(self.machine, fit)
            return self.machine.refresh_settings()

        def done(settings):
            path = save_record(self.win.config.calibration_dir, "load", {
                "points": self.points, "fit": fit, "capacity_n": capacity,
                "gravity": self.win.config.loadcell.gravity,
                "firmware": asdict(self.machine.info) if self.machine.info else None,
                "port": self.machine.port,
            })
            self.win.calibrate_panel.show_settings(settings)
            self.win.inform(self.windowTitle(), f"Saved to the machine.\nRecord: {path}")
            if settings.get("max_load", 0.0) <= 0 and self.win.ask(
                    "Overload cutoff", f"The overload cutoff (max_load) is off. Set it to {0.9 * capacity:g} N "
                                       f"(90 % of the {capacity:g} N capacity in the config)?"):
                self.win.run_async(lambda: self._set_max_load(0.9 * capacity), self.win.calibrate_panel.show_settings)
            self.accept()

        self._set_busy(True)
        self.win.run_async(work, done, self._failed)

    def _set_max_load(self, value: float) -> dict:
        self.machine.set("max_load", value)
        self.machine.save()
        return self.machine.refresh_settings()

    def done(self, result) -> None:  # QDialog.done: runs however the dialog closes
        if self.machine.sim:
            self.machine.sim.set_external_load(0.0)
        super().done(result)


class TravelCalibrationDialog(_Dialog):
    def __init__(self, win):
        super().__init__(win, "Travel calibration (steps/mm)")
        self.old = self.machine.settings.get("steps_per_mm", 80.0)
        self.trials: list[TravelTrial] = []
        self.result_data = None
        self.last_commanded: float | None = None

        self.distance = spin(0.5, 200.0, 20.0, 2, 1.0, "mm")
        self.rate = spin(0.01, 10.0, 1.0, 2, 0.1, "mm/s")
        self.move_btn = button("1.  Move + distance", self.move_forward)
        self.back_btn = button("Move back", self.move_back)
        self.measured = spin(-1000.0, 1000.0, 0.0, 4, 0.01, "mm")
        self.add_btn = button("2.  Add trial", self.add_trial)
        self.trial_list = QtWidgets.QListWidget()
        self.result = QtWidgets.QLabel(f"Current steps/mm: {self.old:.4f}")
        self.result.setWordWrap(True)
        self.save_btn = button("3.  Save to machine", self.save)
        close_btn = button("Close", self.reject)

        form = QtWidgets.QFormLayout()
        form.addRow("Distance", self.distance)
        form.addRow("Rate", self.rate)
        moves = QtWidgets.QHBoxLayout()
        moves.addWidget(self.move_btn, 1)
        moves.addWidget(self.back_btn)
        measured = QtWidgets.QHBoxLayout()
        measured.addWidget(QtWidgets.QLabel("Measured travel"))
        measured.addWidget(self.measured, 1)
        measured.addWidget(self.add_btn)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.save_btn, 1)
        bottom.addWidget(close_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(_intro(
            "Set up calipers or a dial indicator on the crosshead and zero it. Move, measure the real "
            "travel and add it as a trial; move back and repeat for a better average. After saving, set "
            "the position reference again (the mm values change slightly)."))
        layout.addLayout(form)
        layout.addLayout(moves)
        layout.addLayout(measured)
        layout.addWidget(self.trial_list, 1)
        layout.addWidget(self.result)
        layout.addLayout(bottom)
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self._busy
        self.move_btn.setEnabled(idle)
        self.back_btn.setEnabled(idle)
        self.add_btn.setEnabled(idle and self.last_commanded is not None)
        self.save_btn.setEnabled(idle and self.result_data is not None)

    def _move(self, distance: float, fill_measurement: bool) -> None:
        rate = self.rate.value()
        sim = self.machine.sim
        start_true = sim.true_position_mm() if sim else None

        def done(end):
            self._set_busy(False)
            if end.fields.get("reason") != "DONE":
                self.win.warn(self.windowTitle(), f"The move ended early ({end.fields.get('reason')}).")
                return
            if fill_measurement:
                self.last_commanded = end.number("moved")
                shown = round(sim.true_position_mm() - start_true, 4) if sim else self.last_commanded
                self.measured.setValue(shown)
                self.result.setText(f"Commanded {self.last_commanded:.4f} mm. Enter the travel you measured, "
                                    "then Add trial.")
            self._update_buttons()

        self._set_busy(True)
        self.win.run_async(lambda: self.machine.move_and_wait(distance, rate_mm_s=rate), done, self._failed)

    def move_forward(self) -> None:
        self._move(self.distance.value(), True)

    def move_back(self) -> None:
        self._move(-self.distance.value(), False)

    def add_trial(self) -> None:
        commanded, measured = self.last_commanded, self.measured.value()
        if commanded is None or measured == 0:
            return
        self.trials.append(TravelTrial(commanded, measured))
        self.trial_list.addItem(f"commanded {commanded:.4f} mm   measured {measured:.4f} mm   "
                                f"ratio {measured / commanded:.5f}")
        self.last_commanded = None
        try:
            self.result_data = travel_correction(self.old, self.trials)
        except ValueError as e:
            self.result_data = None
            self.result.setText(str(e))
        else:
            r = self.result_data
            text = f"Measured/commanded = {r.ratio:.5f}\nSteps/mm: {r.old_steps_per_mm:.4f} → {r.new_steps_per_mm:.4f}"
            if r.warnings:
                text += "\n\n⚠ " + "\n⚠ ".join(r.warnings)
            self.result.setText(text)
        self._update_buttons()

    def save(self) -> None:
        r = self.result_data
        if r.warnings and not self.win.ask(self.windowTitle(), "There are warnings:\n\n" + "\n\n".join(r.warnings)
                                           + "\n\nSave anyway?"):
            return

        def work():
            apply_travel(self.machine, r.new_steps_per_mm)
            return self.machine.refresh_settings()

        def done(settings):
            path = save_record(self.win.config.calibration_dir, "travel", {
                "trials": self.trials, "result": r, "rate_mm_s": self.rate.value(),
                "firmware": asdict(self.machine.info) if self.machine.info else None,
                "port": self.machine.port,
            })
            self.win.calibrate_panel.show_settings(settings)
            self.win.inform(self.windowTitle(),
                            f"Saved {r.new_steps_per_mm:.4f} steps/mm.\nRecord: {path}\n\n"
                            "Set the position reference again (Jog tab).")
            self.accept()

        self._set_busy(True)
        self.win.run_async(work, done, self._failed)
