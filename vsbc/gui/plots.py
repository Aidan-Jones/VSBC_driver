"""Live plots: load vs extension for the current test, and load / position vs time."""

from __future__ import annotations

from collections import deque

from vsbc.gui.common import GrowingArray, QtWidgets, np, pg
from vsbc.protocol import Sample


class LivePlots(QtWidgets.QTabWidget):
    def __init__(self, window_s: float = 60.0, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=True, background="w", foreground="k")

        # Load vs extension (the test)
        self.test_plot = pg.PlotWidget()
        self.test_plot.setLabel("bottom", "Extension", units="mm")
        self.test_plot.setLabel("left", "Load", units="N")
        self.test_plot.showGrid(x=True, y=True, alpha=0.3)
        self.test_plot.setDownsampling(auto=True, mode="peak")
        self.test_plot.setClipToView(True)
        self.test_curve = self.test_plot.plot(pen=pg.mkPen("#1f5fa8", width=2))
        self.peak_marker = self.test_plot.plot(pen=None, symbol="o", symbolBrush="#c0392b", symbolSize=10)
        self.addTab(self.test_plot, "Load – extension")

        # Load and position vs time (rolling window)
        self.time_widget = pg.GraphicsLayoutWidget()
        self.load_plot = self.time_widget.addPlot(row=0, col=0)
        self.load_plot.setLabel("left", "Load", units="N")
        self.load_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pos_plot = self.time_widget.addPlot(row=1, col=0)
        self.pos_plot.setLabel("left", "Position", units="mm")
        self.pos_plot.setLabel("bottom", "Time", units="s")
        self.pos_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pos_plot.setXLink(self.load_plot)
        self.load_curve = self.load_plot.plot(pen=pg.mkPen("#c0392b", width=2))
        self.pos_curve = self.pos_plot.plot(pen=pg.mkPen("#1e8449", width=2))
        self.addTab(self.time_widget, "Load & position – time")

        n = int(window_s * 100)
        self._t = deque(maxlen=n)
        self._load = deque(maxlen=n)
        self._pos = deque(maxlen=n)
        self._t0_ms: int | None = None
        self._last_ms: int | None = None
        self._ext = GrowingArray()
        self._test_load = GrowingArray()
        self._dirty = False

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
        self.peak_marker.setData([], [])
        self.setCurrentIndex(0)
        self._dirty = True

    def mark_peak(self, extension_mm: float, load_n: float) -> None:
        if np.isfinite(load_n):
            self.peak_marker.setData([extension_mm], [load_n])

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self._t:
            t = np.fromiter(self._t, float, len(self._t))
            self.load_curve.setData(t, np.fromiter(self._load, float, len(self._load)), connect="finite")
            self.pos_curve.setData(t, np.fromiter(self._pos, float, len(self._pos)), connect="finite")
        self.test_curve.setData(self._ext.view(), self._test_load.view(), connect="finite")
