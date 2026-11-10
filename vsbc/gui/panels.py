"""Test, Jog, Console and Firmware tabs. Every panel talks to the machine through
the main window (`win`), which runs slow calls on worker threads."""

from __future__ import annotations

import html
import logging
import math
from pathlib import Path

from vsbc import FIRMWARE_VERSION, PROTOCOL_VERSION
from vsbc.gui.common import (
    QShortcut, QtCore, QtGui, QtWidgets, QuantitySpin, Section, StatCard, button, em, explain_disabled, flow,
    form_layout, high_speed_style, mono_font, next_specimen_id, spin, start_style, stop_color, theme,
)
from vsbc.gui.readiness import ReadyCard, first_todo
from vsbc.procedures import PullTestSpec
from vsbc.units import Units, load_preferences, save_preferences

log = logging.getLogger(__name__)


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
        """The Display tab changed the display units."""
        for box in self.findChildren(QuantitySpin):
            box.apply_units(units)

    def restyle(self) -> None:
        """The theme changed: redraw anything coloured by hand (rich text, item colours)."""


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
        # Quantities hold N / mm; they show and accept the Display tab's choice.
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

        self.specimen.setToolTip("Counts up by itself after each test (A1 → A2); type over it for another specimen.")
        specimen_box = QtWidgets.QGroupBox("Specimen")
        form = form_layout(specimen_box)
        form.addRow("Specimen ID", self.specimen)
        form.addRow("Area (for stress)", self.area)
        form.addRow("Gauge length (for strain)", self.gauge)
        form.addRow("Operator", self.operator)
        form.addRow("Notes", self.notes)

        settings_box = QtWidgets.QGroupBox("Test settings")
        sform = form_layout(settings_box)
        sform.addRow("Rate", self.rate)
        sform.addRow("Max extension", self.max_ext)
        # Set once and rarely changed: folded, with the current values in its header.
        stops = QtWidgets.QWidget()
        stops_form = form_layout(stops)
        stops_form.setContentsMargins(0, 0, 0, 0)
        stops_form.addRow("Stop at load", self.stop_load)
        stops_form.addRow("Break: drop from peak", self.break_drop)
        stops_form.addRow("Break: min peak", self.break_min)
        stops_form.addRow(self.tare)
        stops_form.addRow(self.ret)
        self.stops = Section("Stop conditions", stops, key="test.stops", expanded=False, framed=False)
        sform.addRow(self.stops)
        self._load_settings()
        for box in (self.stop_load, self.break_drop, self.break_min):
            box.valueChanged.connect(lambda _v: self._stops_summary())
        for box in (self.tare, self.ret):
            box.toggled.connect(lambda _on: self._stops_summary())
        self._stops_summary()

        # What is still to do before a test (connect, reference, calibrate, specimen ID, camera).
        self.ready = ReadyCard()
        self.specimen.textChanged.connect(lambda _t: win.refresh_readiness())

        self.start_btn = button("▶  Start test", win.start_test, start_style, em(3.4))
        self.start_hint = QtWidgets.QLabel()
        self.start_hint.setProperty("role", "hint")
        self.start_hint.setWordWrap(True)
        self.high_speed_btn = button("⚡  High-speed camera: save every frame", self.toggle_high_speed,
                                     high_speed_style, em(3))
        self.high_speed_btn.setCheckable(True)
        self.high_speed_btn.setToolTip(
            "Keeps the last seconds held in RAM and saves every camera frame from now until you turn it off "
            "or the test ends. The camera and the test stop together.")
        self.result = QtWidgets.QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setFont(mono_font())
        self.result.setMinimumHeight(em(6))       # grows into the spare height (stretch 1 below)
        self.result.setPlaceholderText("Results appear here when the test ends.")
        self.peak_card = StatCard("Peak load", "load", big=False)
        self.ext_card = StatCard("Extension at peak", "extension", big=False)
        self.stop_card = StatCard("Stopped by", "muted", big=False)
        self.cards = QtWidgets.QWidget()
        self.cards.setLayout(flow(self.peak_card, self.ext_card, self.stop_card))
        self.cards.hide()
        self.open_btn = button("Open results folder", self.open_folder, icon_name="folder")
        self.open_btn.setEnabled(False)
        self.view_btn = button("View results", self.view_results, role="primary")
        self.view_btn.setToolTip("The Results tab (left): camera frames around the crack, plots and summary")
        self.view_btn.setEnabled(False)
        self.folder: Path | None = None
        self._last_result = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.ready)
        layout.addWidget(specimen_box)
        layout.addWidget(settings_box)
        layout.addWidget(self.start_btn)
        layout.addWidget(self.start_hint)
        layout.addWidget(self.high_speed_btn)
        layout.addWidget(self.cards)
        layout.addWidget(self.result, 1)
        layout.addLayout(flow(self.view_btn, self.open_btn))
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

    def _stops_summary(self) -> None:
        u = self.win.units
        stop = self.stop_load.si_value()
        drop = self.break_drop.value()
        parts = [f"stop at {u.format_force(stop)}" if stop > 0 else "no stop load",
                 f"break at {drop:g} % drop" if drop > 0 else "break detection off"]
        if self.tare.isChecked():
            parts.append("tare first")
        if self.ret.isChecked():
            parts.append("return after")
        self.stops.set_summary(", ".join(parts))

    # ---------------------------------------------------------- remembering --
    SAVED = ("rate", "max_ext", "stop_load", "break_min", "area", "gauge")      # QuantitySpin: kept in SI

    def _load_settings(self) -> None:
        """Last session's test settings (Outputs/preferences.json), over config.toml's [test] defaults."""
        saved = load_preferences(self.win.config.preferences_file).get("test")
        if not isinstance(saved, dict):
            return
        try:
            for name in self.SAVED:
                value = saved.get(name)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    getattr(self, name).set_si_value(float(value))
            if isinstance(saved.get("break_drop"), (int, float)):
                self.break_drop.setValue(float(saved["break_drop"]))
            for name in ("tare", "ret"):
                if isinstance(saved.get(name), bool):
                    getattr(self, name).setChecked(saved[name])
            for name in ("specimen", "operator"):
                if isinstance(saved.get(name), str):
                    getattr(self, name).setText(saved[name])
        except Exception:  # noqa: BLE001 - a bad preferences file must not stop the app
            log.exception("could not restore the test settings")

    def save_settings(self) -> None:
        data = {name: getattr(self, name).si_value() for name in self.SAVED}
        data.update(break_drop=self.break_drop.value(), tare=self.tare.isChecked(), ret=self.ret.isChecked(),
                    specimen=self.specimen.text().strip(), operator=self.operator.text().strip())
        save_preferences(self.win.config.preferences_file, {"test": data})

    def show_running(self, spec: PullTestSpec) -> None:
        u = self.win.units
        self._running_id = spec.specimen_id
        self.save_settings()
        self.cards.hide()
        self.result.setPlainText(f"Running {spec.specimen_id} at "
                                 f"{u.length_from_mm(spec.rate_mm_min):g} {u.rate_min_label}...")
        self.open_btn.setEnabled(False)
        self.view_btn.setEnabled(False)

    def show_result(self, result) -> None:
        first = result is not self._last_result
        self._last_result = result
        specimen = getattr(self, "_running_id", None) or self.specimen.text().strip()
        lines = [f"Specimen: {specimen}"]
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
        self.view_btn.setEnabled(result.folder is not None)
        self._show_cards(result)
        if first and result.folder is not None and not result.aborted and self.specimen.text().strip() == specimen:
            following = next_specimen_id(specimen)
            if following != specimen:
                self.specimen.setText(following)
                self.win.console.note(f"Next specimen ID: {following}")

    def _show_cards(self, result) -> None:
        u, s = self.win.units, result.summary
        peak = getattr(s, "peak_load_n", None) if s is not None else None
        ext = getattr(s, "extension_at_peak_mm", None) if s is not None else None
        self.peak_card.set(u.format_force(peak) if peak is not None and math.isfinite(peak) else "—")
        self.ext_card.set(u.format_length(ext) if ext is not None and math.isfinite(ext) else "—")
        reason = "ERROR" if result.error else result.stop_reason
        from vsbc.gui.app import STOP_REASONS        # app imports this module

        self.stop_card.set(STOP_REASONS.get(reason, reason or "—").capitalize() if reason != "ERROR" else "Error",
                           stop_color(reason))
        self.cards.show()

    def restyle(self) -> None:
        if self._last_result is not None:
            self._show_cards(self._last_result)

    def apply_units(self, units: Units) -> None:
        super().apply_units(units)
        self._stops_summary()
        if self._last_result is not None:
            self.show_result(self._last_result)

    def open_folder(self) -> None:
        if self.folder:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.folder)))

    def view_results(self) -> None:
        if self.folder:
            self.win.show_results(self.folder)

    def toggle_high_speed(self) -> None:
        self.win.set_high_speed(self.high_speed_btn.isChecked())

    def show_high_speed(self, on: bool) -> None:
        self.high_speed_btn.blockSignals(True)
        self.high_speed_btn.setChecked(on)
        self.high_speed_btn.blockSignals(False)

    def update_enabled(self, connected: bool, busy: bool) -> None:
        todo = first_todo(self.win.readiness_items)
        self.start_btn.setEnabled(connected and not busy and todo is None)
        if self.start_btn.isEnabled():
            reason = ""
        elif connected and not busy and todo is not None:
            reason = f"Next: {todo.text} (Fix, at the top)."
        else:
            reason = self.win.disabled_reason
        if self.start_hint.text() != reason:
            self.start_hint.setText(reason)
            self.start_hint.setVisible(bool(reason))
        explain_disabled(self.start_btn, reason)
        self.view_btn.setEnabled(self.folder is not None and self.win.runner is None)
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
        steps = flow()
        for i in range(len(self.STEPS)):
            b = QtWidgets.QPushButton()
            b.setCheckable(True)
            b.setChecked(i == 2)
            self.step_group.addButton(b, i)
            steps.addWidget(b)
        self._label_steps(u)

        self.up = button("▲   +  (tension)", lambda: self.jog(+1), min_height=em(3.6), role="tension",
                         tip="Jog one step in the tension direction (Up arrow key)")
        self.down = button("▼   −  (compression)", lambda: self.jog(-1), min_height=em(3.6), role="compression",
                           tip="Jog one step in the compression direction (Down arrow key)")
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

        form = form_layout()
        form.addRow("Jog rate", self.rate)
        form.addRow("Step", steps)
        goto_row = QtWidgets.QHBoxLayout()
        goto_row.addWidget(self.goto_value, 1)
        goto_row.addWidget(self.goto_btn)
        zero_row = flow(self.zero_btn, self.zero_value)

        # The Machine tab shows these two in their own sections (vsbc.gui.machine_panel).
        self.reference_box = QtWidgets.QWidget()
        ref = QtWidgets.QVBoxLayout(self.reference_box)
        ref.setContentsMargins(0, 0, 0, 0)
        hint = QtWidgets.QLabel("Open loop: the position is only known relative to a reference. After "
                                "connecting, jog to the reference position and set it here.")
        hint.setWordWrap(True)
        ref.addWidget(hint)
        ref.addLayout(zero_row)
        ref.addWidget(self.restore_btn)
        self.service_box = QtWidgets.QWidget()
        service = QtWidgets.QVBoxLayout(self.service_box)
        service.setContentsMargins(0, 0, 0, 0)
        service_note = QtWidgets.QLabel("Bring-up only: one motor on its own racks the crosshead if the screws "
                                        "are coupled, and clears the position reference.")
        service_note.setWordWrap(True)
        service.addWidget(service_note)
        service.addWidget(self.motors)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)
        layout.addWidget(self.up)
        layout.addWidget(self.down)
        layout.addLayout(goto_row)
        layout.addWidget(self.home_btn)
        layout.addWidget(self.tare_btn)

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
            explain_disabled(w, self.win.disabled_reason)
        self.restore_btn.setEnabled(ok and self.win.has_saved_position)


# ------------------------------------------------------------------- console --

class ConsolePanel(Panel):
    def __init__(self, win):
        super().__init__(win)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono_font())
        self.text.setMaximumBlockCount(3000)
        self.show_data = QtWidgets.QCheckBox("Show data lines (D ...)")
        self.entry = QtWidgets.QLineEdit()
        self.entry.setPlaceholderText("Command, e.g. STATUS, GET or ?  (any command stops a running move)")
        self.entry.returnPressed.connect(self.send)
        self.send_btn = button("Send", self.send, role="primary")
        self.clear_btn = button("Clear", self.text.clear)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.show_data, 1)
        top.addWidget(self.clear_btn)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.entry, 1)
        row.addWidget(self.send_btn)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.text, 1)
        layout.addLayout(row)

    def _line(self, text: str, color: str | None = None, italic: bool = False) -> None:
        style = "white-space:pre-wrap;" + (f"color:{color};" if color else "") + ("font-style:italic;" if italic else "")
        self.text.appendHtml(f"<span style='{style}'>{html.escape(text)}</span>")

    def append(self, text: str, sent: bool) -> None:
        """Sent commands in the accent colour, errors red, events purple, data lines grey."""
        if text.startswith("D ") and not self.show_data.isChecked():
            return
        t = theme()
        if sent:
            self._line("> " + text, t.accent)
        elif text.startswith("ERR"):
            self._line(text, t.danger)
        elif text.startswith("D "):
            self._line(text, t.muted)
        elif text.startswith("EVT"):
            self._line(text, t.purple)
        elif text.startswith("#"):
            self._line(text, t.muted)
        else:
            self._line(text)

    def note(self, text: str) -> None:
        self._line(f"-- {text}", theme().muted, italic=True)

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
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.compile_btn = button("Compile firmware", self.compile)
        self.upload_btn = button("Compile + upload to the Mega", self.upload, role="primary")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font())
        self.log.setMaximumBlockCount(2000)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.info)
        layout.addWidget(self.compile_btn)
        layout.addWidget(self.upload_btn)
        layout.addWidget(self.log, 1)
        self.update_info()

    def update_info(self) -> None:
        info = self.machine.info if self.machine.connected else None
        t = theme()
        if info:
            connected = html.escape(f"{info.name} {info.version} (protocol {info.protocol})")
            if info.simulated:
                connected += " — simulator"
            if str(info.protocol) != str(PROTOCOL_VERSION):
                mark = f"<b style='color:{t.danger}'>✗ wrong protocol: upload the firmware below</b>"
            elif str(info.version) != str(FIRMWARE_VERSION):
                mark = f"<b style='color:{t.warning}'>⚠ another version (it works; upload to update)</b>"
            else:
                mark = f"<b style='color:{t.success}'>✓ matches</b>"
            connected += f"&nbsp;&nbsp;{mark}"
        else:
            connected = f"<span style='color:{t.muted}'>not connected</span>"
        self.info.setText(
            f"This app expects <b>vsbc_firmware {html.escape(str(FIRMWARE_VERSION))}</b> "
            f"(protocol {html.escape(str(PROTOCOL_VERSION))}).<br>"
            f"Connected: {connected}<br><br>Uploading uses arduino-cli (scripts/install_pi.sh installs it). "
            "The Arduino IDE works too: open firmware/vsbc_firmware/vsbc_firmware.ino."
        )

    def restyle(self) -> None:
        self.update_info()

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
