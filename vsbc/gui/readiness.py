"""The "Ready to test" checklist: what is still to do before a test can start, in the order an
operator does it, each with a Fix button that opens the place where it is done.

`readiness(win)` only reads state (the connection, the Mega's last STATUS reply, the warnings, the
specimen field, the camera); `ReadyCard` shows it at the top of the Test tab. The Mega's own checks
before a test (PullTestRunner.preflight) still run when Start is pressed.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from vsbc.gui.common import Qt, QtWidgets, button, card_style, em, theme, themed

TENSION_TEXT = "Tension is reading negative"


@dataclass(frozen=True)
class Item:
    key: str
    text: str
    level: str                  # ok, todo (must be done before a test) or warn (allowed, but check it)
    fix_label: str = ""
    fix: object = field(default=None, compare=False)     # callable, or None

    @property
    def required(self) -> bool:
        return self.level == "todo"


def readiness(win) -> list[Item]:
    """The checklist for the next test, from what the window knows now."""
    items: list[Item] = []
    machine = win.machine
    reveal = win.reveal
    if not machine.connected:
        items.append(Item("connect", "Connect to the machine", "todo", "Connect",
                          lambda: reveal(win.connect_btn)))
    else:
        items.append(Item("connect", "Connected", "ok"))
        st = machine.last_status
        if st is None:
            items.append(Item("status", "Reading the machine's state...", "todo"))
        else:
            cal = win.calibrate_panel
            if not st.loadcell_ok:
                items.append(Item("loadcell", "The load cell is not responding (check its wiring)", "todo",
                                  "Calibration", lambda: reveal(cal.load_btn)))
            elif not st.calibrated:
                items.append(Item("loadcell", "Calibrate the load cell", "todo", "Calibrate...",
                                  lambda: reveal(cal.load_btn)))
            else:
                items.append(Item("loadcell", "Load cell calibrated", "ok"))
            if any(TENSION_TEXT in w for w in win._warnings):
                items.append(Item("sign", "Tension reads negative: flip the load sign", "warn", "Flip...",
                                  lambda: reveal(cal.flip_btn)))
            jog = win.jog_panel
            if not st.referenced:
                text = "Set the position reference"
                if win.has_saved_position:
                    text += " (or restore the saved one)"
                items.append(Item("reference", text, "todo", "Set...", lambda: reveal(jog.reference_box)))
            else:
                items.append(Item("reference", "Position referenced", "ok"))
            if st.motors != "AB":
                items.append(Item("motors", f"Select both motors (now: {st.motors} only)", "todo", "Motors...",
                                  lambda: reveal(jog.motors)))
    test = win.test_panel
    if test.specimen.text().strip():
        items.append(Item("specimen", f"Specimen {test.specimen.text().strip()}", "ok"))
    else:
        def enter():
            reveal(test.specimen)
            test.specimen.setFocus()

        items.append(Item("specimen", "Enter a specimen ID", "todo", "Enter", enter))
    items += camera_items(win)
    return items


def camera_items(win) -> list[Item]:
    """The camera, when it records with each test: its own checks before a test (CameraPanel.preflight)."""
    panel = getattr(win, "camera_panel", None)
    if win.camera is None or panel is None or not panel.record_with_test.isChecked():
        return []
    try:
        errors, warnings, _record = panel.preflight()
    except Exception:  # noqa: BLE001 - the checklist must never break the window
        return []
    messages = [(m, "todo") for m in errors] + [(m, "warn") for m in warnings]
    if not messages:
        return [Item("camera", "Camera ready: it records with the test", "ok")]
    items = []
    for i, (message, level) in enumerate(messages):
        text = message.split(":")[0] if len(message) > 70 else message
        if "disk space" in message.lower() or "storage" in message.lower():
            items.append(Item(f"camera{i}", text, level, "Outputs", lambda: win.reveal(win.outputs_panel)))
        elif "ROI" in message or "notch" in message:
            items.append(Item(f"camera{i}", text, level, "Edit ROI", lambda: win.reveal(win.camera_view.roi_btn)))
        else:
            items.append(Item(f"camera{i}", text, level, "Camera", lambda: win.reveal(win.camera_view.start_btn)))
    return items


def first_todo(items: list[Item]) -> Item | None:
    return next((i for i in items if i.required), None)


class ReadyCard(QtWidgets.QFrame):
    """The checklist at the top of the Test tab: ✓ done, ● still to do (with Fix), ⚠ worth a look."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.items: list[Item] = []
        self._signature = None
        self.title = QtWidgets.QLabel()
        self.title.setStyleSheet(f"QLabel {{ font-size: {em(1.15)}px; font-weight: bold; }}")
        self.rows = QtWidgets.QGridLayout()
        self.rows.setHorizontalSpacing(em(0.5))
        self.rows.setVerticalSpacing(em(0.15))
        self.rows.setColumnStretch(1, 1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(em(0.6), em(0.4), em(0.6), em(0.4))
        layout.addWidget(self.title)
        layout.addLayout(self.rows)
        self.fix_buttons: dict[str, QtWidgets.QPushButton] = {}
        self.show_items([])

    def show_items(self, items: list[Item], force: bool = False) -> None:
        signature = tuple((i.key, i.text, i.level, i.fix_label) for i in items)
        if signature == self._signature and not force:
            self.items = items          # new callables, same rows
            return
        self._signature = signature
        self.items = items
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w is not None:
                w.hide()                # gone now, not only when the event loop deletes it
                w.deleteLater()
        self.fix_buttons = {}
        t = theme()
        todo = [i for i in items if i.required]
        warn = [i for i in items if i.level == "warn"]
        if not items:
            self.title.setText("")
        elif not todo:
            self.title.setText(f"<span style='color:{t.success}'>✓</span> Ready to test"
                               + (f" <span style='color:{t.warning}'>({len(warn)} to check)</span>" if warn else ""))
        else:
            self.title.setText(f"{len(todo)} step{'s' if len(todo) > 1 else ''} before a test")
        themed(self, lambda c=("warning" if todo else "success"): card_style(getattr(theme(), c)))
        # Ready: one line (and any warnings); not ready: the whole list, so the operator sees the progress.
        shown = [i for i in items if i.level != "ok"] if not todo else items
        for row, item in enumerate(shown):
            colour, mark = {"ok": (t.success, "✓"), "todo": (t.warning, "●"), "warn": (t.warning, "⚠")}[item.level]
            mark_label = QtWidgets.QLabel(f"<span style='color:{colour}; font-weight:bold'>{mark}</span>")
            text = QtWidgets.QLabel(html.escape(item.text))
            text.setWordWrap(True)
            if item.level == "ok":
                text.setStyleSheet(f"QLabel {{ color: {t.muted}; }}")
            elif item.required:
                text.setStyleSheet("QLabel { font-weight: bold; }")
            self.rows.addWidget(mark_label, row, 0, Qt.AlignmentFlag.AlignVCenter)
            self.rows.addWidget(text, row, 1)
            if item.fix is not None and item.level != "ok":
                b = button(item.fix_label or "Fix", lambda _c=False, k=item.key: self._fix(k),
                           role="primary" if item.required else None)
                b.setMinimumHeight(em(2.0))
                self.fix_buttons[item.key] = b
                self.rows.addWidget(b, row, 2, Qt.AlignmentFlag.AlignVCenter)

    def _fix(self, key: str) -> None:
        for item in self.items:
            if item.key == key and callable(item.fix):
                item.fix()
                return


__all__ = ["Item", "readiness", "camera_items", "first_todo", "ReadyCard"]
