"""Qt imports (through pyqtgraph, so PyQt5, PyQt6 or PySide6 all work) and small helpers."""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from vsbc.units import MM_PER, N_PER, Units

Qt = QtCore.Qt
QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut

# Sizing. Everything is sized in pixels from the body text height (`em`), and the text height
# comes from the screen size: the Pi's HDMI and DSI screens often report a wrong physical size,
# which makes point sizes come out tiny or huge.
REFERENCE_SCREEN = (1280, 800)     # font_pt is the text size on this screen; others scale from it
MIN_FONT_PX = 13                   # smallest body text: still easy to read on a 7" 800 × 480 screen
_font_px = 11 * 96 / 72            # apply_style sets it for the screen


def em(x: float = 1.0) -> int:
    """x times the body text height, in pixels."""
    return max(1, round(x * _font_px))


def font_px_for(width: int, height: int, font_pt: float, scale: float = 0.0) -> float:
    """Body text height (px) for a width × height screen: font_pt on the reference screen,
    scaled by the smaller of the two ratios. `scale` > 0 sets the ratio instead."""
    auto = scale <= 0
    if auto:
        scale = min(width / REFERENCE_SCREEN[0], height / REFERENCE_SCREEN[1])
    px = font_pt * 96 / 72 * scale
    return max(MIN_FONT_PX, px) if auto else max(6.0, px)


def screen_size(widget=None) -> tuple[int, int]:
    """Available size (px, without panels) of the widget's screen, else the primary screen."""
    screen = widget.screen() if widget is not None and hasattr(widget, "screen") else None
    screen = screen or QtWidgets.QApplication.primaryScreen()
    if screen is None:
        return REFERENCE_SCREEN
    geo = screen.availableGeometry()
    return geo.width(), geo.height()


def stop_style() -> str:
    return ("QPushButton { background: #c0392b; color: white; font-weight: bold;"
            f" font-size: {em(1.6)}px; border-radius: {em(0.5)}px; padding: {em(0.3)}px {em(1.4)}px; }}"
            "QPushButton:pressed { background: #922b21; }")


def start_style() -> str:
    return ("QPushButton { background: #1e8449; color: white; font-weight: bold;"
            f" border-radius: {em(0.4)}px; padding: {em(0.5)}px; }}"
            "QPushButton:disabled { background: #a9cbb7; }")


def readout_style() -> str:
    return (f"QLabel {{ font-size: {em(2)}px; font-weight: bold;"
            " font-family: Consolas, 'DejaVu Sans Mono', monospace; }")


def caption_style() -> str:
    return f"QLabel {{ color: #555; font-size: {max(11, em(0.85))}px; }}"


def banner_style(critical: bool = False) -> str:
    colours = "background: #f5b7b1; color: #78281f;" if critical else "background: #fdebd0; color: #7e5109;"
    return f"QLabel {{ {colours} padding: {em(0.3)}px {em(0.5)}px; border-radius: {em(0.3)}px; }}"


def high_speed_style() -> str:
    return ("QPushButton { background: #7d3c98; color: white; font-weight: bold;"
            f" border-radius: {em(0.4)}px; padding: {em(0.4)}px {em(0.9)}px; }}"
            "QPushButton:checked { background: #e67e22; }"
            "QPushButton:disabled { background: #c9b3d3; }")


def mono_font() -> QtGui.QFont:
    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
    font.setPixelSize(em(0.95))
    return font


def apply_style(app, font_pt: float, scale: float = 0.0, size: tuple[int, int] | None = None) -> float:
    """Size the text and the controls for the screen (`size`: available px, default the
    primary screen's). Call it before building the window. Returns the text height (px)."""
    global _font_px
    _font_px = font_px_for(*(size or screen_size()), font_pt, scale)
    font = app.font()
    font.setPixelSize(round(_font_px))
    app.setFont(font)
    app.setStyleSheet(
        f"QPushButton {{ min-height: {em(2.3)}px; padding: {em(0.25)}px {em(0.7)}px; }}"
        f"QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {{ min-height: {em(2.1)}px; }}"
        f"QTabBar::tab {{ min-height: {em(2.2)}px; padding: {em(0.25)}px {em(0.9)}px; }}"
        f"QCheckBox, QRadioButton {{ min-height: {em(2)}px; spacing: {em(0.5)}px; }}"
        f"QCheckBox::indicator, QRadioButton::indicator {{ width: {em(1.2)}px; height: {em(1.2)}px; }}"
    )
    return _font_px


def form_layout(parent=None) -> QtWidgets.QFormLayout:
    """A form that puts the field under its label when the two do not fit side by side."""
    form = QtWidgets.QFormLayout(parent) if parent is not None else QtWidgets.QFormLayout()
    form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return form


def narrow_combo(combo: QtWidgets.QComboBox, chars: int = 12) -> QtWidgets.QComboBox:
    """Let a combo box with long items shrink with the panel (instead of widening it)."""
    combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    return combo


class FlowLayout(QtWidgets.QLayout):
    """A row of widgets that wraps onto more rows when the space is too narrow, each row
    stretched to the full width. For button bars that must fit any screen."""

    def __init__(self, parent=None, balanced: bool = False):
        super().__init__(parent)
        self._items: list = []
        self.balanced = balanced        # even rows (4 + 4) rather than full ones (7 + 1)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt names
        self._items.append(item)

    def insertWidget(self, index: int, widget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self._items.insert(index, QtWidgets.QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QtCore.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):  # noqa: N802 - everything in one row
        items = self._visible()
        m = self.contentsMargins()
        width = sum(i.sizeHint().width() for i in items) + self._gap() * max(0, len(items) - 1)
        height = max((i.sizeHint().height() for i in items), default=0)
        return QtCore.QSize(width + m.left() + m.right(), height + m.top() + m.bottom())

    def minimumSize(self):  # noqa: N802 - one widget per row
        m = self.contentsMargins()
        size = QtCore.QSize()
        for item in self._visible():
            size = size.expandedTo(item.minimumSize())
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def _gap(self) -> int:
        return self.spacing() if self.spacing() >= 0 else em(0.4)

    def _visible(self) -> list:
        return [i for i in self._items if not i.isEmpty()]

    def _arrange(self, rect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        gap, width = self._gap(), max(1, area.width())
        sized = [(item, min(item.sizeHint().width(), width)) for item in self._visible()]
        rows = self._rows(sized, width, gap)
        if self.balanced and len(rows) > 1:
            target = (sum(w for _i, w in sized) + gap * (len(sized) - 1)) / len(rows)
            while target < width:
                trial = self._rows(sized, int(target), gap)
                if len(trial) <= len(rows):
                    rows = trial
                    break
                target += em()
        y = area.y()
        for row in rows:
            h = max(item.sizeHint().height() for item, _w in row)
            if apply:
                spare = width - sum(w for _i, w in row) - gap * (len(row) - 1)
                x = area.x()
                for k, (item, w) in enumerate(row):
                    w += spare // len(row) + (1 if k < spare % len(row) else 0)
                    item.setGeometry(QtCore.QRect(x, y, w, h))
                    x += w + gap
            y += h + gap
        content = y - area.y() - (gap if rows else 0)
        return content + m.top() + m.bottom()

    @staticmethod
    def _rows(sized: list, width: int, gap: int) -> list:
        """As many (item, width) as fit in each row, in order."""
        rows, row, used = [], [], 0
        for item, w in sized:
            if row and used + gap + w > width:
                rows.append(row)
                row, used = [], 0
            used += (gap if row else 0) + w
            row.append((item, w))
        if row:
            rows.append(row)
        return rows


def flow(*widgets) -> FlowLayout:
    layout = FlowLayout()
    for w in widgets:
        layout.addWidget(w)
    return layout


def tab_button_style() -> str:
    return (f"QPushButton {{ min-height: {em(2.1)}px; padding: {em(0.2)}px {em(0.6)}px; }}"
            "QPushButton:checked { background: #1f5fa8; color: white; font-weight: bold;"
            f" border: 1px solid #174a84; border-radius: {em(0.3)}px; }}")


class FlowTabs(QtWidgets.QWidget):
    """Tabs whose buttons wrap onto another row instead of scrolling, so on a narrow screen
    every tab stays in view and is a full-size touch target. Takes QTabWidget's calls (those
    this app uses); `setCurrentWidget` also finds a widget inside a page's scroll area."""

    currentChanged = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: list[QtWidgets.QPushButton] = []
        self._current = -1
        self._group = QtWidgets.QButtonGroup(self)
        self._bar = FlowLayout(balanced=True)
        self._bar.setSpacing(em(0.25))
        self._stack = QtWidgets.QStackedWidget()
        self._stack.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self._stack.currentChanged.connect(self._shown)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(em(0.3))
        layout.addLayout(self._bar)
        layout.addWidget(self._stack, 1)

    def addTab(self, widget, text: str) -> int:  # noqa: N802 - Qt names
        return self.insertTab(self.count(), widget, text)

    def insertTab(self, index: int, widget, text: str) -> int:  # noqa: N802
        index = max(0, min(index, self.count()))
        b = QtWidgets.QPushButton(text)
        b.setCheckable(True)
        b.setStyleSheet(tab_button_style())
        b.clicked.connect(lambda _checked=False, b=b: self.setCurrentIndex(self._buttons.index(b)))
        self._group.addButton(b)
        self._buttons.insert(index, b)
        self._bar.insertWidget(index, b)
        self._stack.insertWidget(index, widget)
        self._shown(self._stack.currentIndex())
        return index

    def count(self) -> int:
        return self._stack.count()

    def widget(self, index: int):
        return self._stack.widget(index)

    def indexOf(self, widget) -> int:  # noqa: N802
        for i in range(self.count()):
            page = self.widget(i)
            if page is widget or (isinstance(page, QtWidgets.QScrollArea) and page.widget() is widget):
                return i
        return -1

    def tabText(self, index: int) -> str:  # noqa: N802
        return self._buttons[index].text()

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802
        self._buttons[index].setText(text)

    def currentIndex(self) -> int:  # noqa: N802
        return self._stack.currentIndex()

    def currentWidget(self):  # noqa: N802
        return self._stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= index < self.count():
            self._stack.setCurrentIndex(index)

    def setCurrentWidget(self, widget) -> None:  # noqa: N802
        self.setCurrentIndex(self.indexOf(widget))

    def _shown(self, index: int) -> None:
        for i, b in enumerate(self._buttons):
            b.setChecked(i == index)       # exclusive group: checking one unchecks the others
        if index != self._current:
            self._current = index
            self.currentChanged.emit(index)


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
           "GrowingArray", "QuantitySpin", "nice_step", "apply_style", "exec_app", "em", "font_px_for", "screen_size",
           "stop_style", "start_style", "readout_style", "caption_style", "banner_style", "high_speed_style",
           "mono_font", "form_layout", "narrow_combo", "FlowLayout", "flow", "FlowTabs", "tab_button_style"]
