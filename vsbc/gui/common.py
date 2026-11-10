"""Qt imports (through pyqtgraph, so PyQt5, PyQt6 or PySide6 all work) and small helpers."""

from __future__ import annotations

import html
import math
import re
import weakref
from dataclasses import dataclass

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


# ------------------------------------------------------------------ theme --
# One palette for the whole GUI, light or dark (Display tab). Widgets name a colour by role
# (theme().danger), never by hex, so switching the theme recolours everything.

@dataclass(frozen=True)
class Theme:
    name: str
    bg: str             # window
    surface: str        # group boxes, inputs, cards
    surface_alt: str    # alternate rows, headers, pressed
    border: str
    text: str
    muted: str          # captions, disabled text
    accent: str         # selection, checked buttons, group titles
    accent_hover: str
    on_accent: str      # text on accent / coloured fills
    success: str
    warning: str
    danger: str
    danger_pressed: str
    purple: str
    orange: str
    warn_bg: str        # warning banner
    warn_fg: str
    crit_bg: str        # critical banner
    crit_fg: str
    plot_bg: str
    plot_fg: str
    load: str           # plot series, and the readout cards that match them
    position: str
    extension: str
    strain: str


LIGHT = Theme(
    name="light", bg="#eef1f5", surface="#ffffff", surface_alt="#e4e9f0", border="#c3ccd8", text="#1c2430",
    muted="#5f6b7a", accent="#1f5fa8", accent_hover="#2a72c7", on_accent="#ffffff", success="#1e8449",
    warning="#c47f0a", danger="#c0392b", danger_pressed="#922b21", purple="#7d3c98", orange="#e67e22",
    warn_bg="#fdebd0", warn_fg="#6e4708", crit_bg="#f5b7b1", crit_fg="#6b231b", plot_bg="#ffffff",
    plot_fg="#333333", load="#c0392b", position="#1e8449", extension="#1f5fa8", strain="#8e44ad",
)
DARK = Theme(
    name="dark", bg="#181c21", surface="#232930", surface_alt="#2c333c", border="#3d4651", text="#e4e8ed",
    muted="#97a3b0", accent="#4a90d9", accent_hover="#62a3ea", on_accent="#ffffff", success="#27ae60",
    warning="#f0a330", danger="#e74c3c", danger_pressed="#b03a2e", purple="#a569bd", orange="#f39c4a",
    warn_bg="#4a3a12", warn_fg="#ffdc8f", crit_bg="#5c1f1a", crit_fg="#ffbdb3", plot_bg="#1e2329",
    plot_fg="#c9d1d9", load="#ff6f61", position="#3ddc84", extension="#5aa9ff", strain="#c39bd3",
)
THEMES = {"light": LIGHT, "dark": DARK}
_theme = LIGHT

# Drawn on camera frames, which stay black in both themes: the same colours in both.
ROI_COLOR = "#00c8ff"
SEED_COLOR = "#2ecc71"
NOTCH_COLOR = "#ff4df0"
PLACEHOLDER_COLOR = "#dddddd"
TIP_STYLES = {"image tracker": ("+", "#ff3b30"), "opening method": ("x", "#00e5ff"), "by hand": ("o", "#ffd60a")}

# Move-end reasons that are a safety stop rather than the test's own end.
SAFETY_STOPS = {"OVERLOAD", "LOADCELL", "WATCHDOG", "TIMEOUT", "LIMIT", "ESTOP"}


def theme() -> Theme:
    return _theme


def state_color(state: str) -> str:
    """The machine-state pill: grey offline, blue idle, amber moving, green testing."""
    t = _theme
    return {"OFFLINE": t.muted, "IDLE": t.accent, "MOVE": t.warning, "RUN": t.success,
            "TEST": t.success}.get(state, t.purple)


def level_color(level: str) -> str:
    """ok / warn / error / pending (checks, disk space, results)."""
    t = _theme
    return {"ok": t.success, "warn": t.warning, "low": t.warning, "error": t.danger, "critical": t.danger,
            "pending": t.muted}.get(level, t.text)


def stop_color(reason: str | None) -> str:
    """A test's stop reason: green for its own end (break, stop load, max extension), red for a safety stop."""
    if not reason:
        return _theme.muted
    if reason in SAFETY_STOPS or reason in ("ERROR", "STOP"):
        return _theme.danger
    return _theme.success


def dot(color: str, text: str = "") -> str:
    """Rich text: a coloured ● and the (escaped) text."""
    return f"<span style='color:{color}'>●</span>&nbsp;{html.escape(text)}" if text else \
        f"<span style='color:{color}'>●</span>"


# Widgets and plots whose colours come from the theme: recoloured by set_theme.
_sheets: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_plots: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_pens: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def themed(widget, sheet):
    """widget.setStyleSheet(sheet()), and again whenever the theme changes. `sheet` must not hold
    the widget itself (the registry keys on the widget weakly)."""
    _sheets[widget] = sheet
    widget.setStyleSheet(sheet())
    return widget


def themed_pen(item, color: str, width: float = 2.0):
    """A pyqtgraph curve or line drawn in theme().<color>, recoloured with the theme."""
    _pens[item] = (color, width)
    item.setPen(pg.mkPen(getattr(_theme, color), width=width))
    return item


def style_plot(plot, label_px: int | None = None):
    """A PlotWidget, PlotItem or GraphicsLayoutWidget in the theme's plot colours (background,
    axes, grid, legend), and again whenever the theme changes."""
    _plots[plot] = label_px
    _restyle_plot(plot)
    return plot


def _restyle_plot(plot) -> None:
    t = _theme
    if hasattr(plot, "setBackground"):
        plot.setBackground(t.plot_bg)
    item = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
    if not hasattr(item, "getAxis"):
        return
    for side in ("left", "bottom", "right", "top"):
        axis = item.getAxis(side)
        axis.setPen(pg.mkPen(t.plot_fg))
        axis.setTextPen(pg.mkPen(t.plot_fg))
    legend = getattr(item, "legend", None)
    if legend is not None:
        legend.setLabelTextColor(t.plot_fg)


def plot_label_style() -> dict:
    """Axis label style for setLabel (which replaces the axis's own style, colour included)."""
    return {"font-size": f"{em(0.95)}px", "color": _theme.plot_fg}


def stop_style() -> str:
    t = _theme
    return (f"QPushButton {{ background: {t.danger}; color: white; font-weight: bold; border: none;"
            f" font-size: {em(1.6)}px; border-radius: {em(0.5)}px; padding: {em(0.3)}px {em(1.4)}px; }}"
            f"QPushButton:hover {{ background: {t.danger}; border: 2px solid {t.text}; }}"
            f"QPushButton:pressed {{ background: {t.danger_pressed}; }}")


def start_style() -> str:
    t = _theme
    return (f"QPushButton {{ background: {t.success}; color: white; font-weight: bold; border: none;"
            f" border-radius: {em(0.4)}px; padding: {em(0.5)}px; font-size: {em(1.15)}px; }}"
            f"QPushButton:hover {{ border: 2px solid {t.text}; }}"
            f"QPushButton:disabled {{ background: {t.surface_alt}; color: {t.muted}; }}")


def readout_style() -> str:
    return (f"QLabel {{ font-size: {em(2)}px; font-weight: bold;"
            " font-family: Consolas, 'DejaVu Sans Mono', monospace; }")


def pill_style(color: str) -> str:
    """The STATE readout: white bold text on a rounded coloured pill."""
    return (f"QLabel {{ font-size: {em(1.5)}px; font-weight: bold; color: white; background: {color};"
            f" border-radius: {em(0.5)}px; padding: {em(0.1)}px {em(0.7)}px; }}")


def caption_style() -> str:
    return (f"QLabel {{ color: {_theme.muted}; font-size: {max(11, em(0.85))}px; font-weight: bold;"
            " letter-spacing: 1px; }")


def status_style() -> str:
    return f"QLabel {{ font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: {max(11, em(0.85))}px; }}"


def countdown_style() -> str:
    return (f"QLabel {{ background: {_theme.purple}; color: white; font-size: {em(1.4)}px; font-weight: bold; "
            f"padding: {em(0.4)}px; border-radius: {em(0.3)}px; }}")


def banner_style(critical: bool = False) -> str:
    t = _theme
    bg, fg, edge = (t.crit_bg, t.crit_fg, t.danger) if critical else (t.warn_bg, t.warn_fg, t.warning)
    return (f"QFrame#banner {{ background: {bg}; border-left: {em(0.35)}px solid {edge};"
            f" border-radius: {em(0.3)}px; }}"
            f"QFrame#banner QLabel {{ color: {fg}; background: transparent; }}"
            f"QFrame#banner QPushButton {{ color: {fg}; background: transparent; border: none;"
            f" font-size: {em(1.3)}px; font-weight: bold; min-height: {em(1.8)}px; }}")


def high_speed_style() -> str:
    t = _theme
    return (f"QPushButton {{ background: {t.purple}; color: white; font-weight: bold; border: none;"
            f" border-radius: {em(0.4)}px; padding: {em(0.4)}px {em(0.9)}px; }}"
            f"QPushButton:checked {{ background: {t.orange}; }}"
            f"QPushButton:disabled {{ background: {t.surface_alt}; color: {t.muted}; }}")


def card_style(color: str) -> str:
    """A readout card: the surface colour with a coloured top edge (the matching plot series)."""
    t = _theme
    return (f"QFrame#card {{ background: {t.surface}; border: 1px solid {t.border};"
            f" border-top: {max(3, em(0.3))}px solid {color}; border-radius: {em(0.4)}px; }}"
            "QFrame#card QLabel { background: transparent; border: none; }")


def mono_font() -> QtGui.QFont:
    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
    font.setPixelSize(em(0.95))
    return font


def palette(t: Theme | None = None) -> QtGui.QPalette:
    t = t or _theme
    p = QtGui.QPalette()
    role, group, C = QtGui.QPalette.ColorRole, QtGui.QPalette.ColorGroup, QtGui.QColor
    for r, colour in ((role.Window, t.bg), (role.WindowText, t.text), (role.Base, t.surface),
                      (role.AlternateBase, t.surface_alt), (role.ToolTipBase, t.surface_alt),
                      (role.ToolTipText, t.text), (role.Text, t.text), (role.Button, t.surface),
                      (role.ButtonText, t.text), (role.BrightText, t.danger), (role.Highlight, t.accent),
                      (role.HighlightedText, t.on_accent), (role.Link, t.accent), (role.Light, t.surface),
                      (role.Midlight, t.surface_alt), (role.Mid, t.border), (role.Dark, t.border),
                      (role.Shadow, "#000000")):
        p.setColor(r, C(colour))
    if hasattr(role, "PlaceholderText"):
        p.setColor(role.PlaceholderText, C(t.muted))
    for r in (role.WindowText, role.Text, role.ButtonText):
        p.setColor(group.Disabled, r, C(t.muted))
    p.setColor(group.Disabled, role.Button, C(t.surface_alt))
    p.setColor(group.Disabled, role.Base, C(t.surface_alt))
    return p


def app_sheet() -> str:
    """The application style sheet: sizes from the text height (`em`), colours from the theme."""
    t = _theme
    r = em(0.35)
    return (
        # Sizes (touch targets)
        f"QPushButton {{ min-height: {em(2.3)}px; padding: {em(0.25)}px {em(0.7)}px; }}"
        f"QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {{ min-height: {em(2.1)}px; }}"
        f"QTabBar::tab {{ min-height: {em(2.2)}px; padding: {em(0.25)}px {em(0.9)}px; }}"
        f"QCheckBox, QRadioButton {{ min-height: {em(2)}px; spacing: {em(0.5)}px; }}"
        f"QCheckBox::indicator, QRadioButton::indicator {{ width: {em(1.2)}px; height: {em(1.2)}px; }}"
        # Buttons
        f"QPushButton {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border};"
        f" border-radius: {r}px; }}"
        f"QPushButton:hover {{ border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.surface_alt}; }}"
        f"QPushButton:checked {{ background: {t.accent}; color: {t.on_accent}; border-color: {t.accent};"
        " font-weight: bold; }"
        f"QPushButton:disabled {{ background: {t.surface_alt}; color: {t.muted}; border-color: {t.surface_alt}; }}"
        f"QPushButton[role=\"primary\"] {{ background: {t.accent}; color: {t.on_accent}; border-color: {t.accent};"
        " font-weight: bold; }"
        f"QPushButton[role=\"primary\"]:hover {{ background: {t.accent_hover}; }}"
        f"QPushButton[role=\"success\"] {{ background: {t.success}; color: white; border-color: {t.success};"
        " font-weight: bold; }"
        f"QPushButton[role=\"danger\"] {{ color: {t.danger}; border-color: {t.danger}; }}"
        f"QPushButton[role=\"danger\"]:hover {{ background: {t.danger}; color: white; }}"
        f"QPushButton[role=\"tension\"] {{ border-left: {em(0.4)}px solid {t.success}; font-weight: bold; }}"
        f"QPushButton[role=\"compression\"] {{ border-left: {em(0.4)}px solid {t.warning}; font-weight: bold; }}"
        f"QPushButton[role=\"primary\"]:disabled, QPushButton[role=\"success\"]:disabled,"
        f" QPushButton[role=\"danger\"]:disabled {{ background: {t.surface_alt}; color: {t.muted};"
        f" border-color: {t.surface_alt}; }}"
        # Inputs. Spin and combo boxes keep Fusion's own drawing (from the palette): a style-sheet
        # border would make Qt drop their arrows.
        f"QLineEdit {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border};"
        f" border-radius: {r}px; padding: 0 {em(0.35)}px; selection-background-color: {t.accent};"
        f" selection-color: {t.on_accent}; }}"
        f"QLineEdit:focus {{ border: 2px solid {t.accent}; }}"
        f"QLineEdit:disabled {{ background: {t.surface_alt}; color: {t.muted}; }}"
        f"QPlainTextEdit, QTextEdit, QListWidget {{ background: {t.surface}; color: {t.text};"
        f" border: 1px solid {t.border}; border-radius: {r}px; }}"
        # Group boxes: a titled card
        f"QGroupBox {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {em(0.45)}px;"
        f" margin-top: {em(1.3)}px; padding: {em(0.5)}px {em(0.4)}px {em(0.3)}px {em(0.4)}px; }}"
        f"QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: {em(0.6)}px;"
        f" padding: 0 {em(0.3)}px; color: {t.accent}; font-weight: bold; }}"
        # Progress, tables, tool tips, status bar, splitter
        f"QProgressBar {{ background: {t.surface_alt}; color: {t.text}; border: 1px solid {t.border};"
        f" border-radius: {r}px; text-align: center; min-height: {em(1.4)}px; }}"
        f"QProgressBar::chunk {{ background: {t.accent}; border-radius: {max(1, r - 1)}px; }}"
        f"QTableView {{ background: {t.surface}; alternate-background-color: {t.surface_alt}; color: {t.text};"
        f" gridline-color: {t.border}; border: 1px solid {t.border}; selection-background-color: {t.accent};"
        f" selection-color: {t.on_accent}; }}"
        f"QHeaderView::section {{ background: {t.surface_alt}; color: {t.text}; font-weight: bold; border: none;"
        f" border-right: 1px solid {t.border}; border-bottom: 1px solid {t.border}; padding: {em(0.25)}px {em(0.4)}px; }}"
        f"QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.accent}; padding: {em(0.3)}px; }}"
        f"QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; }}"
        "QStatusBar::item { border: none; }"
        f"QSplitter::handle {{ background: {t.surface_alt}; }}"
        f"QSplitter::handle:hover {{ background: {t.accent}; }}"
        f"QStackedWidget#pages {{ background: {t.bg}; border: 1px solid {t.border}; border-radius: {r}px; }}"
        f"QLabel[role=\"hint\"] {{ color: {t.muted}; }}"
        # Collapsible sections: an accent header line over a framed body
        f"QPushButton[role=\"section\"] {{ text-align: left; font-weight: bold; color: {t.accent};"
        f" background: transparent; border: none; border-bottom: 1px solid {t.border}; border-radius: 0;"
        f" padding: {em(0.2)}px {em(0.3)}px; min-height: {em(2.1)}px; }}"
        f"QPushButton[role=\"section\"]:hover {{ background: {t.surface_alt}; }}"
        f"QFrame#sectionBody {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {em(0.45)}px; }}"
        f"QFrame#sectionBody > QLabel {{ background: transparent; border: none; }}"
    )


def apply_style(app, font_pt: float, scale: float = 0.0, size: tuple[int, int] | None = None,
                theme_name: str = "light") -> float:
    """Size the text and the controls for the screen (`size`: available px, default the
    primary screen's), in the light or dark theme. Call it before building the window.
    Returns the text height (px)."""
    global _font_px
    _font_px = font_px_for(*(size or screen_size()), font_pt, scale)
    font = app.font()
    font.setPixelSize(round(_font_px))
    app.setFont(font)
    set_theme(app, theme_name)
    return _font_px


def set_theme(app, name: str) -> Theme:
    """Switch to the light or dark theme now: the style, palette and style sheet, then every
    widget and plot registered with themed / themed_pen / style_plot."""
    global _theme
    _theme = THEMES.get(name, LIGHT)
    if app is not None:
        if "fusion" in QtWidgets.QStyleFactory.keys() or "Fusion" in QtWidgets.QStyleFactory.keys():
            app.setStyle("Fusion")      # the same look on Windows, Linux and the Pi
        app.setPalette(palette())
        app.setStyleSheet(app_sheet())
    pg.setConfigOptions(background=_theme.plot_bg, foreground=_theme.plot_fg)
    for registry, apply in ((_sheets, lambda w, sheet: w.setStyleSheet(sheet())),
                            (_plots, lambda p, _px: _restyle_plot(p)),
                            (_pens, lambda i, cw: i.setPen(pg.mkPen(getattr(_theme, cw[0]), width=cw[1])))):
        for obj, value in list(registry.items()):
            try:
                apply(obj, value)
            except RuntimeError:        # the Qt object is gone
                registry.pop(obj, None)
    return _theme


def set_role(widget, role: str | None):
    """Style a button by role (primary, success, danger, tension, compression) from the app style sheet."""
    if widget.property("role") != role:
        widget.setProperty("role", role or "")
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    return widget


_ICONS = {"play": "SP_MediaPlay", "stop": "SP_MediaStop", "reload": "SP_BrowserReload",
          "folder": "SP_DirOpenIcon", "trash": "SP_TrashIcon", "save": "SP_DialogSaveButton",
          "warning": "SP_MessageBoxWarning", "info": "SP_MessageBoxInformation",
          "critical": "SP_MessageBoxCritical", "drive": "SP_DriveHDIcon", "cancel": "SP_DialogCancelButton",
          "apply": "SP_DialogApplyButton", "video": "SP_MediaPlay", "file": "SP_FileIcon"}


def icon(name: str) -> QtGui.QIcon:
    """One of Qt's built-in icons (no image files to ship)."""
    style = QtWidgets.QApplication.style()
    pixmap = getattr(QtWidgets.QStyle.StandardPixmap, _ICONS[name], None)
    return style.standardIcon(pixmap) if style is not None and pixmap is not None else QtGui.QIcon()


def explain_disabled(widget, reason: str) -> None:
    """While the widget is greyed out its tool tip says why; its own tool tip comes back when it is enabled."""
    own = widget.property("own_tip")
    if own is None:
        own = widget.toolTip()
        widget.setProperty("own_tip", own)
    if widget.isEnabled() or not reason:
        tip = own
    else:
        tip = f"{reason}\n\n{own}" if own else reason
    if widget.toolTip() != tip:
        widget.setToolTip(tip)


def next_specimen_id(text: str) -> str:
    """The next specimen ID: the last number counts up, keeping its zero padding (A1 → A2,
    S-09 → S-10, T7-b → T8-b). Without a number it stays the same."""
    m = re.search(r"(\d+)(\D*)$", text)
    if not m:
        return text
    digits = m.group(1)
    return text[:m.start(1)] + str(int(digits) + 1).zfill(len(digits)) + m.group(2)


class StatCard(QtWidgets.QFrame):
    """A caption over a value, on a card whose top edge shows a colour (theme role or #hex)."""

    def __init__(self, caption: str, color: str = "accent", big: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.caption = QtWidgets.QLabel(caption.upper())
        themed(self.caption, caption_style)
        self.value = QtWidgets.QLabel("—")
        self.value.setStyleSheet(readout_style() if big else
                                 f"QLabel {{ font-size: {em(1.25)}px; font-weight: bold; }}")
        self.value.setWordWrap(not big)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(em(0.6), em(0.35), em(0.6), em(0.35))
        layout.setSpacing(0)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        self._color = None
        self.set_color(color)

    def set_color(self, color: str) -> None:
        """A theme role name (load, success, ...) or a fixed #rrggbb."""
        if color != self._color:
            self._color = color
            themed(self, lambda c=color: card_style(getattr(_theme, c, c)))

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        if color is not None:
            self.set_color(color)


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
    t = _theme
    return (f"QPushButton {{ min-height: {em(2.1)}px; padding: {em(0.2)}px {em(0.6)}px; }}"
            f"QPushButton:checked {{ background: {t.accent}; color: {t.on_accent}; font-weight: bold;"
            f" border: 1px solid {t.accent}; border-radius: {em(0.3)}px; }}")


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
        self._stack.setObjectName("pages")
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
        themed(b, tab_button_style)
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
        """The page that is `widget` or holds it (inside a scroll area, a section or nested tabs)."""
        for i in range(self.count()):
            page = self.widget(i)
            if page is widget or (widget is not None and page.isAncestorOf(widget)):
                return i
        return -1

    def tabText(self, index: int) -> str:  # noqa: N802
        return self._buttons[index].text()

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802
        self._buttons[index].setText(text)

    def isTabEnabled(self, index: int) -> bool:  # noqa: N802
        return self._buttons[index].isEnabled()

    def setTabEnabled(self, index: int, enabled: bool) -> None:  # noqa: N802
        """Grey out a tab's button; if it is the one showing, show the first enabled tab instead."""
        if not 0 <= index < self.count():
            return
        self._buttons[index].setEnabled(enabled)
        if not enabled and self.currentIndex() == index:
            for i, b in enumerate(self._buttons):
                if b.isEnabled():
                    self.setCurrentIndex(i)
                    break

    def currentIndex(self) -> int:  # noqa: N802
        return self._stack.currentIndex()

    def currentWidget(self):  # noqa: N802
        return self._stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= index < self.count() and self._buttons[index].isEnabled():
            self._stack.setCurrentIndex(index)

    def setCurrentWidget(self, widget) -> None:  # noqa: N802
        self.setCurrentIndex(self.indexOf(widget))

    def _shown(self, index: int) -> None:
        for i, b in enumerate(self._buttons):
            b.setChecked(i == index)       # exclusive group: checking one unchecks the others
        if index != self._current:
            self._current = index
            self.currentChanged.emit(index)


class Section(QtWidgets.QWidget):
    """A titled part of a page that folds away: "▾ Title" over its body, "▸ Title · summary" when
    folded. Rarely used settings start folded; the window remembers each section's state by `key`."""

    toggled = QtCore.Signal(bool)

    def __init__(self, title: str, body: QtWidgets.QWidget | None = None, key: str = "", expanded: bool = True,
                 framed: bool = True, parent=None):
        super().__init__(parent)
        self.title, self.key, self._summary, self._expanded = title, key, "", expanded
        self.header = QtWidgets.QPushButton()
        self.header.setProperty("role", "section")
        self.header.clicked.connect(self.toggle)
        self.summary = QtWidgets.QLabel()          # the values at a glance while folded (wraps on narrow screens)
        self.summary.setProperty("role", "hint")
        self.summary.setWordWrap(True)
        self.summary.setContentsMargins(em(1.4), 0, 0, 0)
        self.frame = QtWidgets.QFrame()
        if framed:
            self.frame.setObjectName("sectionBody")
        inner = QtWidgets.QVBoxLayout(self.frame)
        inner.setContentsMargins(*([em(0.5)] * 4) if framed else (0, em(0.2), 0, 0))
        self.body = body if body is not None else QtWidgets.QWidget()
        inner.addWidget(self.body)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(em(0.25))
        layout.addWidget(self.header)
        layout.addWidget(self.summary)
        layout.addWidget(self.frame)
        self.set_expanded(expanded, emit=False)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, on: bool, emit: bool = True) -> None:
        changed = on != self._expanded
        self._expanded = on
        self.frame.setVisible(on)
        self._label()
        if emit and changed:
            self.toggled.emit(on)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_summary(self, text: str) -> None:
        """Shown after the title while the section is folded (the current values, at a glance)."""
        self._summary = text
        self._label()

    def _label(self) -> None:
        self.header.setText(f"{'▾' if self._expanded else '▸'}  {self.title}")
        self.summary.setText(self._summary)
        self.summary.setVisible(bool(self._summary) and not self._expanded)


def flash(widget, ms: int = 1500) -> None:
    """Draw the eye to a widget: an accent glow for a moment."""
    effect = QtWidgets.QGraphicsDropShadowEffect()
    effect.setColor(QtGui.QColor(_theme.accent))
    effect.setBlurRadius(em(1.6))
    effect.setOffset(0, 0)
    widget.setGraphicsEffect(effect)

    def clear():
        try:
            if widget.graphicsEffect() is effect:
                widget.setGraphicsEffect(None)
        except RuntimeError:        # the widget is gone
            pass

    QtCore.QTimer.singleShot(ms, clear)


def reveal(widget, glow: bool = True) -> None:
    """Make a widget visible wherever it is: choose its tab in every enclosing set of tabs, unfold every
    enclosing section, scroll it into view and (glow) flash it."""
    chain, w = [], widget
    while w is not None:
        chain.append(w)
        w = w.parentWidget()
    for w in reversed(chain):              # outermost first
        if isinstance(w, FlowTabs):
            index = w.indexOf(widget)
            if index >= 0:
                w.setCurrentIndex(index)
        elif isinstance(w, Section) and w is not widget and not w.is_expanded():
            w.set_expanded(True)

    def scroll():
        try:
            for w in chain:
                if isinstance(w, QtWidgets.QScrollArea) and w.isAncestorOf(widget):
                    w.ensureWidgetVisible(widget, em(1), em(3))
            if glow:
                flash(widget)
        except RuntimeError:
            pass

    QtCore.QTimer.singleShot(0, scroll)       # after the layout has placed the unfolded widgets


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


def button(text: str, slot=None, style=None, min_height: int | None = None, *, role: str | None = None,
           icon_name: str | None = None, tip: str = "") -> QtWidgets.QPushButton:
    """A push button. `style`: a style sheet, or a function returning one (re-applied with the theme);
    `role`: primary / success / danger / tension / compression; `icon_name`: one of Qt's own icons."""
    b = QtWidgets.QPushButton(text)
    if slot is not None:
        b.clicked.connect(slot)
    if callable(style):
        themed(b, style)
    elif style:
        b.setStyleSheet(style)
    if min_height:
        b.setMinimumHeight(min_height)
    if role:
        b.setProperty("role", role)
    if icon_name:
        b.setIcon(icon(icon_name))
        b.setIconSize(QtCore.QSize(em(1.1), em(1.1)))
    if tip:
        b.setToolTip(tip)
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
           "mono_font", "form_layout", "narrow_combo", "FlowLayout", "flow", "FlowTabs", "tab_button_style",
           "Theme", "LIGHT", "DARK", "THEMES", "theme", "set_theme", "palette", "app_sheet", "themed", "themed_pen",
           "style_plot", "plot_label_style", "state_color", "level_color", "stop_color", "dot", "pill_style",
           "card_style", "status_style", "countdown_style", "set_role", "icon", "explain_disabled",
           "next_specimen_id", "StatCard", "SAFETY_STOPS", "ROI_COLOR", "SEED_COLOR", "NOTCH_COLOR",
           "PLACEHOLDER_COLOR", "TIP_STYLES", "Section", "flash", "reveal"]
