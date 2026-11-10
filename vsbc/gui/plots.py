"""The Live tab: load vs extension for the current test, and load / position vs time
(two buttons at the top switch between them).

Data are kept in newtons and millimetres and converted to the display units
when drawn, so changing the units redraws everything at once.
"""

from __future__ import annotations

from collections import deque

from vsbc.gui.common import (
    FlowTabs, GrowingArray, QtGui, em, np, pg, plot_label_style, style_plot, theme, themed_pen,
)
from vsbc.protocol import Sample
from vsbc.units import Units

IDLE_WINDOW_S = 20.0      # load / position vs time before the first test: only the last 20 s


class LivePlots(FlowTabs):
    def __init__(self, window_s: float = 60.0, parent=None, units: Units | None = None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=True, background=theme().plot_bg, foreground=theme().plot_fg)
        self.units = units or Units()

        # Load vs extension (the test)
        self.test_plot = pg.PlotWidget()
        self.test_plot.showGrid(x=True, y=True, alpha=0.3)
        self.test_plot.setDownsampling(auto=True, mode="peak")
        self.test_plot.setClipToView(True)
        self.test_curve = themed_pen(self.test_plot.plot(), "extension", 2)
        self.peak_marker = self.test_plot.plot(pen=None, symbol="o", symbolBrush="#e74c3c", symbolPen="w",
                                               symbolSize=em(0.9))
        self.peak_text = pg.TextItem("", color="#e74c3c", anchor=(0, 1.1))
        self.test_plot.addItem(self.peak_text)
        self.addTab(self.test_plot, "Load – extension")

        # Load and position vs time (rolling window)
        self.time_widget = pg.GraphicsLayoutWidget()
        self.load_plot = self.time_widget.addPlot(row=0, col=0)
        self.load_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pos_plot = self.time_widget.addPlot(row=1, col=0)
        self.pos_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pos_plot.setXLink(self.load_plot)
        self.load_curve = themed_pen(self.load_plot.plot(), "load", 2)
        self.pos_curve = themed_pen(self.pos_plot.plot(), "position", 2)
        self.addTab(self.time_widget, "Load && position – time")   # && = a literal & in a tab label

        n = int(window_s * 100)
        self._t = deque(maxlen=n)
        self._load = deque(maxlen=n)
        self._pos = deque(maxlen=n)
        self._t0_ms: float | None = None
        self._last_ms: float | None = None
        self._ext = GrowingArray()
        self._test_load = GrowingArray()
        self._peak: tuple[float, float] | None = None
        self._test_started = False        # until the first test: show only the last IDLE_WINDOW_S
        self._dirty = False
        # Axis numbers and labels in the GUI's text size (pyqtgraph's own default is small and fixed).
        tick_font = QtGui.QFont()
        tick_font.setPixelSize(max(12, em(0.9)))
        for plot in (self.test_plot.getPlotItem(), self.load_plot, self.pos_plot):
            for side in ("left", "bottom"):
                plot.getAxis(side).setStyle(tickFont=tick_font)
        for plot in (self.test_plot, self.time_widget, self.load_plot, self.pos_plot):
            style_plot(plot)
        self.apply_units(self.units)

    def restyle(self) -> None:
        """The theme changed: the axis labels carry their own colour."""
        self.apply_units(self.units)

    def apply_units(self, units: Units) -> None:
        """Relabel the axes and redraw in the new units. The unit goes in the label, without
        pyqtgraph's automatic SI prefix (which would turn inches into "min" and mm into "mmm")."""
        self.units = units
        for plot, side, text, unit in ((self.test_plot.getPlotItem(), "bottom", "Extension", units.length),
                                       (self.test_plot.getPlotItem(), "left", "Load", units.force),
                                       (self.load_plot, "left", "Load", units.force),
                                       (self.pos_plot, "left", "Position", units.length)):
            axis = plot.getAxis(side)
            axis.enableAutoSIPrefix(False)
            plot.setLabel(side, f"{text} ({unit})", **plot_label_style())
        self.pos_plot.setLabel("bottom", "Time (s)", **plot_label_style())
        if self._peak is not None:
            self.mark_peak(*self._peak)
        self._dirty = True

    def add_sample(self, s: Sample, extension_mm: float | None = None) -> None:
        if self._last_ms is None or s.t_ms < self._last_ms:
            self._t0_ms = s.t_ms          # first sample, or the Mega was reset
            self._t.clear()
            self._load.clear()
            self._pos.clear()
        self._last_ms = s.t_ms
        self._t.append((s.t_ms - self._t0_ms) / 1000.0)
        self._load.append(s.load_n)
        self._pos.append(s.pos_mm)
        if extension_mm is not None:
            self._ext.append(extension_mm)
            self._test_load.append(s.load_n)
        self._dirty = True

    def begin_test(self) -> None:
        self._test_started = True
        self._ext.clear()
        self._test_load.clear()
        self._peak = None
        self.peak_marker.setData([], [])
        self.peak_text.setText("")
        # Show load - extension, unless another tab (the camera) was chosen on purpose.
        if self.currentWidget() in (self.test_plot, self.time_widget):
            self.setCurrentWidget(self.test_plot)
        self._dirty = True

    def mark_peak(self, extension_mm: float, load_n: float) -> None:
        if np.isfinite(load_n):
            self._peak = (extension_mm, load_n)
            x, y = self.units.length_from_mm(extension_mm), self.units.force_from_n(load_n)
            self.peak_marker.setData([x], [y])
            self.peak_text.setText(f"peak {self.units.format_force(load_n)}")
            self.peak_text.setPos(x, y)

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        u = self.units
        if self._t:
            t = np.fromiter(self._t, float, len(self._t))
            load = np.fromiter(self._load, float, len(self._load))
            pos = np.fromiter(self._pos, float, len(self._pos))
            if not self._test_started:
                first = int(np.searchsorted(t, t[-1] - IDLE_WINDOW_S))
                t, load, pos = t[first:], load[first:], pos[first:]
            self.load_curve.setData(t, u.force_from_n(load), connect="finite")
            self.pos_curve.setData(t, u.length_from_mm(pos), connect="finite")
        if u.is_default:
            self.test_curve.setData(self._ext.view(), self._test_load.view(), connect="finite")
        else:
            self.test_curve.setData(u.length_from_mm(self._ext.view()), u.force_from_n(self._test_load.view()),
                                    connect="finite")
