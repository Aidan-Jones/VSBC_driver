"""Live plots: load vs extension for the current test, and load / position vs time.

Data are kept in newtons and millimetres and converted to the display units
when drawn, so changing the units redraws everything at once.
"""

from __future__ import annotations

from collections import deque

from vsbc.gui.common import FlowTabs, GrowingArray, QtGui, em, np, pg
from vsbc.protocol import Sample
from vsbc.units import Units


class LivePlots(FlowTabs):
    def __init__(self, window_s: float = 60.0, parent=None, units: Units | None = None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=True, background="w", foreground="k")
        self.units = units or Units()

        # Load vs extension (the test)
        self.test_plot = pg.PlotWidget()
        self.test_plot.showGrid(x=True, y=True, alpha=0.3)
        self.test_plot.setDownsampling(auto=True, mode="peak")
        self.test_plot.setClipToView(True)
        self.test_curve = self.test_plot.plot(pen=pg.mkPen("#1f5fa8", width=2))
        self.peak_marker = self.test_plot.plot(pen=None, symbol="o", symbolBrush="#c0392b", symbolSize=em(0.75))
        self.addTab(self.test_plot, "Load – extension")

        # Load and position vs time (rolling window)
        self.time_widget = pg.GraphicsLayoutWidget()
        self.load_plot = self.time_widget.addPlot(row=0, col=0)
        self.load_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pos_plot = self.time_widget.addPlot(row=1, col=0)
        self.pos_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pos_plot.setXLink(self.load_plot)
        self.load_curve = self.load_plot.plot(pen=pg.mkPen("#c0392b", width=2))
        self.pos_curve = self.pos_plot.plot(pen=pg.mkPen("#1e8449", width=2))
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
        self._dirty = False
        # Axis numbers and labels in the GUI's text size (pyqtgraph's own default is small and fixed).
        tick_font = QtGui.QFont()
        tick_font.setPixelSize(max(12, em(0.9)))
        for plot in (self.test_plot.getPlotItem(), self.load_plot, self.pos_plot):
            for side in ("left", "bottom"):
                plot.getAxis(side).setStyle(tickFont=tick_font)
        self.pos_plot.setLabel("bottom", "Time", units="s", **self._label_style())
        self.apply_units(self.units)

    @staticmethod
    def _label_style() -> dict:
        return {"font-size": f"{em(0.95)}px"}

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
            plot.setLabel(side, f"{text} ({unit})", **self._label_style())
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
        self._ext.clear()
        self._test_load.clear()
        self._peak = None
        self.peak_marker.setData([], [])
        # Show load - extension, unless another tab (the camera) was chosen on purpose.
        if self.currentWidget() in (self.test_plot, self.time_widget):
            self.setCurrentWidget(self.test_plot)
        self._dirty = True

    def mark_peak(self, extension_mm: float, load_n: float) -> None:
        if np.isfinite(load_n):
            self._peak = (extension_mm, load_n)
            self.peak_marker.setData([self.units.length_from_mm(extension_mm)], [self.units.force_from_n(load_n)])

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        u = self.units
        if self._t:
            t = np.fromiter(self._t, float, len(self._t))
            self.load_curve.setData(t, u.force_from_n(np.fromiter(self._load, float, len(self._load))),
                                    connect="finite")
            self.pos_curve.setData(t, u.length_from_mm(np.fromiter(self._pos, float, len(self._pos))),
                                   connect="finite")
        if u.is_default:
            self.test_curve.setData(self._ext.view(), self._test_load.view(), connect="finite")
        else:
            self.test_curve.setData(u.length_from_mm(self._ext.view()), u.force_from_n(self._test_load.view()),
                                    connect="finite")
