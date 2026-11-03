"""Test, Jog, Console and Firmware tabs. Every panel talks to the machine through
the main window (`win`), which runs slow calls on worker threads."""

from __future__ import annotations

from pathlib import Path

from vsbc import FIRMWARE_VERSION, PROTOCOL_VERSION
from vsbc.gui.common import (
    HIGH_SPEED_STYLE, START_STYLE, QShortcut, QtCore, QtGui, QtWidgets, QuantitySpin, button, spin,
)
from vsbc.procedures import PullTestSpec
from vsbc.units import Units

MONO = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)


class Panel(QtWidgets.QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win

    @property
    def machine(self):
        return self.win.machine

    def update_enabled(self, connected: bool, busy: bool) -> None:
        pass

    def on_connected(self) -> None:
        pass

    def apply_units(self, units: Units) -> None:
        """The Units tab changed the display units."""
        for box in self.findChildren(QuantitySpin):
            box.apply_units(units)


# ---------------------------------------------------------------------- test --

class TestPanel(Panel):
    __test__ = False   # not a pytest class

    def __init__(self, win):
        super().__init__(win)
        d = win.config.test
        u = win.units
        self.specimen = QtWidgets.QLineEdit()
        self.specimen.setPlaceholderText("e.g. A1")
        self.operator = QtWidgets.QLineEdit()
        self.notes = QtWidgets.QLineEdit()
        # Quantities hold N / mm; they show and accept the Units tab's choice.
        self.rate = QuantitySpin("rate_min", 0.12, 600.0, d.rate_mm_min, u, 2, 1.0)
        self.max_ext = QuantitySpin("length", 0.1, 1000.0, d.max_extension_mm, u, 2, 1.0)
        self.stop_load = QuantitySpin("force", 0.0, 1e6, d.stop_load_n, u, 1, 10.0, special="off")
        self.break_drop = spin(0.0, 99.0, d.break_drop_pct, 1, 5.0, "%")
        self.break_drop.setSpecialValueText("off")
        self.break_min = QuantitySpin("force", 0.0, 1e6, d.break_min_n, u, 1, 1.0)
        self.area = QuantitySpin("area", 0.0, 1e6, 0.0, u, 3, 1.0, special="not set")
        self.gauge = QuantitySpin("length", 0.0, 1e4, 0.0, u, 2, 1.0, special="not set")
        self.tare = QtWidgets.QCheckBox("Tare the load cell before starting")
        self.tare.setChecked(d.tare_before)
        self.ret = QtWidgets.QCheckBox("Return to the start position afterwards")
        self.ret.setChecked(d.return_after)

        form = QtWidgets.QFormLayout()
        form.addRow("Specimen ID", self.specimen)
        form.addRow("Rate", self.rate)
        form.addRow("Max extension", self.max_ext)
        form.addRow("Stop at load", self.stop_load)
        form.addRow("Break: drop from peak", self.break_drop)
        form.addRow("Break: min peak", self.break_min)
        form.addRow("Area (for stress)", self.area)
        form.addRow("Gauge length (for strain)", self.gauge)
        form.addRow("Operator", self.operator)
        form.addRow("Notes", self.notes)
        form.addRow(self.tare)
        form.addRow(self.ret)

        self.start_btn = button("▶  Start test", win.start_test, START_STYLE, 56)
        self.high_speed_btn = button("⚡  High-speed camera: save every frame", self.toggle_high_speed,
                                     HIGH_SPEED_STYLE, 48)
        self.high_speed_btn.setCheckable(True)
        self.high_speed_btn.setToolTip(
            "Keeps the last seconds held in RAM and saves every camera frame from now until you turn it off "
            "or the test ends. The camera and the test stop together.")
        self.result = QtWidgets.QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setFont(MONO)
        self.result.setMinimumHeight(170)
        self.result.setPlaceholderText("Results appear here when the test ends.")
        self.open_btn = button("Open results folder", self.open_folder)
        self.open_btn.setEnabled(False)
        self.folder: Path | None = None
        self._last_result = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.start_btn)
        layout.addWidget(self.high_speed_btn)
        layout.addWidget(self.result, 1)
        layout.addWidget(self.open_btn)
        self.high_speed_btn.setVisible(win.config.camera.enabled)

    def build_spec(self) -> PullTestSpec:
        return PullTestSpec(
            specimen_id=self.specimen.text().strip(),
            rate_mm_min=self.rate.si_value(),
            max_extension_mm=self.max_ext.si_value(),
            stop_load_n=self.stop_load.si_value(),
            break_drop_pct=self.break_drop.value(),
            break_min_n=self.break_min.si_value(),
            area_mm2=self.area.si_value() or None,
            gauge_length_mm=self.gauge.si_value() or None,
            tare_before=self.tare.isChecked(),
            return_after=self.ret.isChecked(),
            return_rate_mm_s=self.win.config.test.jog_rate_mm_s,
            operator=self.operator.text().strip(),
            notes=self.notes.text().strip(),
        )

    def show_running(self, spec: PullTestSpec) -> None:
        u = self.win.units
        self.result.setPlainText(f"Running {spec.specimen_id} at "
                                 f"{u.length_from_mm(spec.rate_mm_min):g} {u.rate_min_label}...")
        self.open_btn.setEnabled(False)

    def show_result(self, result) -> None:
        self._last_result = result
        lines = [f"Specimen: {self.specimen.text().strip()}"]
        if result.error:
            lines.append(f"ERROR: {result.error}")
        for warning in getattr(result, "warnings", []) or []:
            lines.append(f"WARNING: {warning}")
        if result.summary:
            lines.append(result.summary.to_text(self.win.units))
        lines.append(f"\nFiles: {result.folder}")
        self.result.setPlainText("\n".join(lines))
        self.folder = result.folder
        self.open_btn.setEnabled(result.folder is not None)

    def apply_units(self, units: Units) -> None:
        super().apply_units(units)
        if self._last_result is not None:
            self.show_result(self._last_result)

    def open_folder(self) -> None:
        if self.folder:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.folder)))

    def toggle_high_speed(self) -> None:
        self.win.set_high_speed(self.high_speed_btn.isChecked())

    def show_high_speed(self, on: bool) -> None:
        self.high_speed_btn.blockSignals(True)
        self.high_speed_btn.setChecked(on)
        self.high_speed_btn.blockSignals(False)

    def update_enabled(self, connected: bool, busy: bool) -> None:
        self.start_btn.setEnabled(connected and not busy)
        camera = self.win.camera
        self.high_speed_btn.setEnabled(camera is not None and camera.state == "recording")


# ----------------------------------------------------------------------- jog --

class JogPanel(Panel):
    STEPS = (0.01, 0.1, 1.0, 10.0)                      # mm (metric units show the same steps)
    STEPS_IN = (0.001, 0.01, 0.1, 0.5)                  # inches

    def __init__(self, win):
        super().__init__(win)
        u = win.units
        self.rate = QuantitySpin("rate_s", 0.01, 10.0, win.config.test.jog_rate_mm_s, u, 2, 0.5)
        self.step_group = QtWidgets.QButtonGroup(self)
        steps = QtWidgets.QHBoxLayout()
        for i in range(len(self.STEPS)):
            b = QtWidgets.QPushButton()
            b.setCheckable(True)
            b.setChecked(i == 2)
            self.step_group.addButton(b, i)
            steps.addWidget(b)
        self._label_steps(u)

        self.up = button("▲   +  (tension)", lambda: self.jog(+1), min_height=64)
        self.down = button("▼   −  (compression)", lambda: self.jog(-1), min_height=64)
        self.goto_value = QuantitySpin("length", -10000.0, 10000.0, 0.0, u, 3, 1.0)
        self.goto_btn = button("Go to", self.go_to)
        self.home_btn = button("Home (go to 0)", self.home)
        self.tare_btn = button("Tare load cell", self.tare)
        self.zero_value = QuantitySpin("length", -10000.0, 10000.0, 0.0, u, 3, 1.0)
        self.zero_btn = button("Set current position to", self.set_position)
        self.restore_btn = button("Restore last saved position", self.restore)
        self.motors = QtWidgets.QComboBox()
        self.motors.addItems(["Both motors (AB)", "Motor A only (service)", "Motor B only (service)"])
        self.motors.currentIndexChanged.connect(self.select_motors)

        form = QtWidgets.QFormLayout()
        form.addRow("Jog rate", self.rate)
        form.addRow("Step", steps)
        goto_row = QtWidgets.QHBoxLayout()
        goto_row.addWidget(self.goto_value, 1)
        goto_row.addWidget(self.goto_btn)
        zero_row = QtWidgets.QHBoxLayout()
        zero_row.addWidget(self.zero_btn)
        zero_row.addWidget(self.zero_value, 1)

        ref_box = QtWidgets.QGroupBox("Position reference")
        ref = QtWidgets.QVBoxLayout(ref_box)
        hint = QtWidgets.QLabel("Open loop: the position is only known relative to a reference. After "
                                "connecting, jog to the reference position and set it here.")
        hint.setWordWrap(True)
        ref.addWidget(hint)
        ref.addLayout(zero_row)
        ref.addWidget(self.restore_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.up)
        layout.addWidget(self.down)
        layout.addLayout(goto_row)
        layout.addWidget(self.home_btn)
        layout.addWidget(self.tare_btn)
        layout.addWidget(ref_box)
        layout.addWidget(QtWidgets.QLabel("Motors"))
        layout.addWidget(self.motors)
        layout.addStretch(1)

        for key, sign in (("Up", +1), ("Down", -1)):
            shortcut = QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda s=sign: self.jog(s) if self.up.isEnabled() else None)

    def _steps_mm(self, units: Units) -> tuple[float, ...]:
        return tuple(s * 25.4 for s in self.STEPS_IN) if units.length == "in" else self.STEPS

    def _label_steps(self, units: Units) -> None:
        for i, step in enumerate(self._steps_mm(units)):
            self.step_group.button(i).setText(f"{units.length_from_mm(step):g} {units.length}")

    def apply_units(self, units: Units) -> None:
        super().apply_units(units)
        self._label_steps(units)

    def step(self) -> float:
        """The selected step in mm."""
        return self._steps_mm(self.win.units)[max(self.step_group.checkedId(), 0)]

    def jog(self, sign: int) -> None:
        distance, rate = sign * self.step(), self.rate.si_value()
        self.win.run_async(lambda: self.machine.move_by(distance, rate_mm_s=rate))

    def go_to(self) -> None:
        target, rate = self.goto_value.si_value(), self.rate.si_value()
        self.win.run_async(lambda: self.machine.move_to(target, rate_mm_s=rate))

    def home(self) -> None:
        rate = self.rate.si_value()
        self.win.run_async(lambda: self.machine.home(rate_mm_s=rate))

    def tare(self) -> None:
        self.win.run_async(self.machine.tare, lambda r: self.win.status(
            f"Tared: offset {r.get('offset')} counts, noise {r.get('noise')} counts"))

    def set_position(self) -> None:
        value, u = self.zero_value.si_value(), self.win.units
        if not self.win.ask("Set position",
                            f"Make the current crosshead position {u.format_length(value)}?\n\nThe travel limits "
                            "are measured from this reference, so only do it at the reference position."):
            return
        self.win.run_async(lambda: self.machine.zero(value),
                           lambda r: self.win.status(
                               f"Position set to {self.win.units.format_length(r.number('pos'))} (referenced)"))

    def restore(self) -> None:
        saved = self.machine.saved_position()
        if not saved:
            self.win.inform("Restore position", "No saved position.")
            return
        u = self.win.units
        if self.win.ask("Restore position",
                        f"Set the position to {u.format_length(float(saved['pos_mm']))}, as saved at "
                        f"{saved.get('saved', '?')}?\n\n"
                        "Only correct if the crosshead has not moved since (by hand or otherwise)."):
            self.win.run_async(lambda: self.machine.zero(float(saved["pos_mm"])),
                               lambda r: self.win.status(
                                   f"Position restored to {self.win.units.format_length(r.number('pos'))}"))

    def select_motors(self, index: int) -> None:
        which = self.MOTOR_CHOICES[index]
        if which != "AB" and not self.win.ask(
                "Single-motor service mode",
                f"Only driver {which} will get step pulses. If the screws are coupled to the crosshead "
                "this racks it. Moves in this mode clear the position reference.\n\nContinue?"):
            self.motors.blockSignals(True)
            self.motors.setCurrentIndex(0)
            self.motors.blockSignals(False)
            return
        self.win.run_async(lambda: self.machine.select_motors(which),
                           lambda r: self.win.status(f"Motors: {r.get('motors')}"))

    MOTOR_CHOICES = ("AB", "A", "B")

    def show_motors(self, which: str) -> None:
        """Show the Mega's motor selection without sending MOTORS again."""
        self.motors.blockSignals(True)
        self.motors.setCurrentIndex(self.MOTOR_CHOICES.index(which) if which in self.MOTOR_CHOICES else 0)
        self.motors.blockSignals(False)

    def on_connected(self) -> None:
        self.show_motors("AB")
        # The Mega does not always reset when the port opens: it may still be on one motor.
        self.win.run_async(self.machine.status, lambda st: self.show_motors(st.motors), lambda _e: None,
                           busy=False)

    def update_enabled(self, connected: bool, busy: bool) -> None:
        ok = connected and not busy
        for w in (self.up, self.down, self.goto_btn, self.home_btn, self.tare_btn, self.zero_btn, self.motors):
            w.setEnabled(ok)
        self.restore_btn.setEnabled(ok and self.win.has_saved_position)


# ------------------------------------------------------------------- console --

class ConsolePanel(Panel):
    def __init__(self, win):
        super().__init__(win)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(MONO)
        self.text.setMaximumBlockCount(3000)
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.show_data = QtWidgets.QCheckBox("Show data lines (D ...)")
        self.entry = QtWidgets.QLineEdit()
        self.entry.setPlaceholderText("Command, e.g. STATUS, GET or ?  (any command stops a running move)")
        self.entry.returnPressed.connect(self.send)
        self.send_btn = button("Send", self.send)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.entry, 1)
        row.addWidget(self.send_btn)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.show_data)
        layout.addWidget(self.text, 1)
        layout.addLayout(row)

    def append(self, text: str, sent: bool) -> None:
        if text.startswith("D ") and not self.show_data.isChecked():
            return
        self.text.appendPlainText(("> " if sent else "") + text)

    def note(self, text: str) -> None:
        self.text.appendPlainText(f"-- {text}")

    def send(self) -> None:
        cmd = self.entry.text().strip()
        if cmd and self.machine.connected:
            self.machine.send_raw(cmd)
            self.entry.clear()

    def update_enabled(self, connected: bool, busy: bool) -> None:
        self.send_btn.setEnabled(connected)
        self.entry.setEnabled(connected)


# ------------------------------------------------------------------ firmware --

class FirmwarePanel(Panel):
    def __init__(self, win):
        super().__init__(win)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.compile_btn = button("Compile firmware", self.compile)
        self.upload_btn = button("Compile + upload to the Mega", self.upload)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(MONO)
        self.log.setMaximumBlockCount(2000)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.info)
        layout.addWidget(self.compile_btn)
        layout.addWidget(self.upload_btn)
        layout.addWidget(self.log, 1)
        self.update_info()

    def update_info(self) -> None:
        info = self.machine.info if self.machine.connected else None
        if info:
            connected = f"{info.name} {info.version} (protocol {info.protocol})"
            if info.simulated:
                connected += " — simulator"
        else:
            connected = "not connected"
        self.info.setText(
            f"This app expects vsbc_firmware {FIRMWARE_VERSION} (protocol {PROTOCOL_VERSION}).\n"
            f"Connected: {connected}\n\nUploading uses arduino-cli (scripts/install_pi.sh installs it). "
            "The Arduino IDE works too: open firmware/vsbc_firmware/vsbc_firmware.ino."
        )

    def _log(self, line: str) -> None:
        self.win.post(self.log.appendPlainText, line)

    def compile(self) -> None:
        from vsbc.firmware import compile_firmware

        self.log.clear()
        self.win.run_async(lambda: compile_firmware(log=self._log), lambda _r: self._log("Compile OK."))

    def upload(self) -> None:
        from vsbc.firmware import upload_firmware
        from vsbc.link import find_port

        port = self.win.selected_port()
        if port == "SIM":
            self.win.warn("Upload", "Pick the Mega's serial port (not the simulator) first.")
            return
        if not self.win.ask("Upload firmware", "Compile and upload the firmware to the Mega? "
                                               "The app disconnects during the upload."):
            return
        self.log.clear()
        was_connected = self.machine.connected

        def work():
            if self.machine.connected:
                self.machine.disconnect()
            target = port or find_port()
            upload_firmware(target, log=self._log)
            return target

        def done(target):
            self._log(f"Uploaded to {target}.")
            self.win.on_disconnected()
            if was_connected:
                self.win.connect_machine(target, sim=False)

        self.win.run_async(work, done)

    def on_connected(self) -> None:
        self.update_info()

    def update_enabled(self, connected: bool, busy: bool) -> None:
        self.compile_btn.setEnabled(not busy)
        self.upload_btn.setEnabled(not busy and self.win.runner is None)
