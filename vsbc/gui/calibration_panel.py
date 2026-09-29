"""Calibrate tab: shows the settings stored on the Mega and runs the calibration
wizards, the direction check, travel limits and the overload cutoff."""

from __future__ import annotations

from vsbc.calibration import latest_record
from vsbc.gui.common import QtWidgets, button, fmt
from vsbc.gui.panels import Panel


class CalibratePanel(Panel):
    FIELDS = (
        ("steps_per_mm", "Steps per mm", 4, ""),
        ("load_scale", "Load scale", 4, "counts/N"),
        ("load_offset", "Load zero", 0, "counts"),
        ("max_load", "Overload cutoff", 1, "N"),
        ("min_pos", "Lower travel limit", 3, "mm"),
        ("max_pos", "Upper travel limit", 3, "mm"),
        ("dir_invert", "Direction inverted", 0, ""),
        ("invert_b", "Motor B inverted", 0, ""),
    )

    def __init__(self, win):
        super().__init__(win)
        self.values: dict[str, QtWidgets.QLabel] = {}
        box = QtWidgets.QGroupBox("Stored on the Mega (EEPROM)")
        form = QtWidgets.QFormLayout(box)
        for key, label, _digits, _unit in self.FIELDS:
            self.values[key] = QtWidgets.QLabel("—")
            form.addRow(label, self.values[key])
        self.records = QtWidgets.QLabel("")
        self.records.setWordWrap(True)
        form.addRow("Last calibration", self.records)

        self.load_btn = button("Load cell calibration...", self.load_wizard)
        self.travel_btn = button("Travel calibration (steps/mm)...", self.travel_wizard)
        self.dir_btn = button("Direction check...", self.direction_check)
        self.lower_btn = button("Set lower travel limit here", lambda: self.set_limit("min_pos"))
        self.upper_btn = button("Set upper travel limit here", lambda: self.set_limit("max_pos"))
        self.max_load_btn = button("Set overload cutoff...", self.set_max_load)
        self.refresh_btn = button("Refresh", self.refresh)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(box)
        for b in (self.load_btn, self.travel_btn, self.dir_btn, self.lower_btn, self.upper_btn,
                  self.max_load_btn, self.refresh_btn):
            layout.addWidget(b)
        layout.addStretch(1)

    # ------------------------------------------------------------- display --
    def refresh(self) -> None:
        if self.machine.connected:
            self.win.run_async(self.machine.refresh_settings, self.show_settings, busy=False)

    def on_connected(self) -> None:
        self.show_settings(self.machine.settings)

    def show_settings(self, settings: dict) -> None:
        for key, _label, digits, unit in self.FIELDS:
            value = settings.get(key)
            if key == "load_scale" and value == 0:
                text = "not calibrated"
            elif key == "max_load" and value == 0:
                text = "off"
            elif key in ("dir_invert", "invert_b") and value is not None:
                text = "yes" if value else "no"
            else:
                text = fmt(value, digits, unit)
            self.values[key].setText(text)
        parts = []
        for kind, name in (("load", "load cell"), ("travel", "travel")):
            rec = latest_record(self.win.config.calibration_dir, kind)
            parts.append(f"{name}: {rec[1].get('created', '?') if rec else 'none'}")
        self.records.setText("; ".join(parts))

    # ------------------------------------------------------------- wizards --
    def load_wizard(self) -> None:
        from vsbc.gui.wizards import LoadCalibrationDialog

        LoadCalibrationDialog(self.win).open_dialog()

    def travel_wizard(self) -> None:
        from vsbc.gui.wizards import TravelCalibrationDialog

        TravelCalibrationDialog(self.win).open_dialog()

    # ----------------------------------------------------- direction check --
    def direction_check(self) -> None:
        """Move +1 mm; if that was not tension, flip dir_invert and come back."""
        rate = self.win.jog_panel.rate.value()
        if not self.win.ask("Direction check", "The crosshead will move +1 mm, which should be the tension "
                                               "direction (grips moving apart). Watch which way it goes."):
            return
        start = self.machine.latest.pos_mm if self.machine.latest else 0.0
        self.win.run_async(lambda: self.machine.move_and_wait(1.0, rate_mm_s=rate),
                           lambda end: self._direction_moved(end, rate, start))

    def _direction_moved(self, end, rate: float, start: float) -> None:
        if end.fields.get("reason") != "DONE":
            self.win.warn("Direction check", f"The move ended early ({end.fields.get('reason')}).")
            return
        if self.win.ask("Direction check", "Did it move in the tension direction?"):
            self.win.run_async(lambda: self.machine.move_and_wait(-1.0, rate_mm_s=rate),
                               lambda _end: self._offer_motor_b(rate))
            return
        flip = 0 if self.machine.settings.get("dir_invert") else 1

        def fix():
            referenced = self.machine.status().referenced
            self.machine.set("dir_invert", flip)
            self.machine.save()
            # With the direction flipped, + now moves the other way: back to the start.
            self.machine.move_and_wait(1.0, rate_mm_s=rate)
            if referenced:
                self.machine.zero(start)
            return self.machine.refresh_settings()

        def done(settings):
            self.show_settings(settings)
            self.win.inform("Direction check", "Direction flipped and saved. The crosshead is back where it started.")
            self._offer_motor_b(rate)

        self.win.run_async(fix, done)

    def _offer_motor_b(self, rate: float) -> None:
        if not self.win.ask("Motor B", "Check motor B on its own too?\n\nOnly do this if the two screws can "
                                       "turn independently (crosshead removed or unclamped)."):
            return

        def move_b():
            self.machine.select_motors("B")
            return self.machine.move_and_wait(1.0, rate_mm_s=rate)

        def moved(_end):
            same = self.win.ask("Motor B", "Did motor B's screw move in the tension direction?")
            flip = None if same else (0 if self.machine.settings.get("invert_b") else 1)

            def finish():
                if flip is not None:
                    self.machine.set("invert_b", flip)
                    self.machine.save()
                # Back to the start: -1 mm, or +1 mm once B's direction has been flipped.
                self.machine.move_and_wait(-1.0 if flip is None else 1.0, rate_mm_s=rate)
                self.machine.select_motors("AB")
                return self.machine.refresh_settings()

            def done(settings):
                self.show_settings(settings)
                self.win.inform("Motor B", ("Motor B's direction was flipped and saved. " if flip is not None else "")
                                + "Single-motor moves clear the position reference: set it again (Jog tab).")

            self.win.run_async(finish, done)

        self.win.run_async(move_b, moved)

    # -------------------------------------------------------------- limits --
    def set_limit(self, key: str) -> None:
        s = self.machine.latest
        if s is None:
            return
        name = "lower" if key == "min_pos" else "upper"
        if not self.win.ask("Travel limit", f"Save {s.pos_mm:.3f} mm as the {name} travel limit?\n\n"
                                            "Limits are measured from the position reference."):
            return

        def work():
            self.machine.set(key, s.pos_mm)
            self.machine.save()
            return self.machine.refresh_settings()

        self.win.run_async(work, self.show_settings)

    def set_max_load(self) -> None:
        current = self.machine.settings.get("max_load", 0.0) or 0.9 * self.win.config.loadcell.capacity_n
        value, ok = QtWidgets.QInputDialog.getDouble(
            self, "Overload cutoff", "Any move stops when |load| reaches (N, 0 = off):", current, 0.0, 1e6, 1)
        if not ok:
            return

        def work():
            self.machine.set("max_load", value)
            self.machine.save()
            return self.machine.refresh_settings()

        self.win.run_async(work, self.show_settings)

    def update_enabled(self, connected: bool, busy: bool) -> None:
        ok = connected and not busy
        for b in (self.load_btn, self.travel_btn, self.dir_btn, self.lower_btn, self.upper_btn, self.max_load_btn):
            b.setEnabled(ok)
        self.refresh_btn.setEnabled(connected)
