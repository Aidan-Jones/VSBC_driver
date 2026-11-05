"""The DIC tab (left side, after Results): choose a test, check it, run DIC
on it, and look at the displacement, strain and crack-tip results.

Sub-tabs:
    Run        what to run and its parameters, the checklist, Run / Cancel, progress
    DIC        pyvale's displacement (u, v or |d|) over the frame
    Strain     pyvale's strain (eps_yy, eps_xx, eps_xy) over the frame, mean strain vs time
    Crack tip  both trackers' tips and your own marks (click to mark), crack length vs time
    Log        this test's post-processing log

The jobs are the same ones that run after each test (vsbc.camera.postprocess):
low priority, one at a time, paused while a test records. Like Results, the
tab is greyed out while a test or a camera recording runs.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vsbc.camera import dic_results as dr
from vsbc.camera import tiff
from vsbc.camera.postprocess import Options
from vsbc.camera.results import RunResults, load_results
from vsbc.camera.roi import load_roi, outline, save_roi
from vsbc.camera.scripts import check_scripts, literal_settings
from vsbc.gui.camera_view import status_style
from vsbc.gui.common import (
    FlowTabs, Qt, QtCore, QtGui, QtWidgets, button, em, flow, form_layout, mono_font, np, pg, spin,
)
from vsbc.storage import disk_status, list_outputs
from vsbc.units import Units

log = logging.getLogger(__name__)

CACHE_FRAMES = 48
PLAY_FPS = (5, 15, 30)
OVERLAY_ALPHA = 140
LEVEL_COLOR = {"ok": "#1e8449", "warn": "#b9770e", "error": "#c0392b", "pending": "#7f8c8d"}
TIP_STYLES = {"image tracker": ("+", "#ff3b30"), "opening method": ("x", "#00e5ff"), "by hand": ("o", "#ffd60a")}

_env_check: tuple[bool, str] | None = None      # the camera environment check, once per session


def colormap(name: str):
    for candidate in (name, "viridis"):
        try:
            return pg.colormap.get(candidate)
        except Exception:  # noqa: BLE001 - an older pyqtgraph without this map
            continue
    return None


def grid_overlay(f: dr.Field, lo: float, hi: float, cmap) -> tuple[np.ndarray, QtCore.QRectF] | None:
    """RGBA image of a field (transparent where there is no value) and where it sits on the frame."""
    if f is None or not f.ok or cmap is None:
        return None
    lut = cmap.getLookupTable(nPts=256, alpha=False)
    span = hi - lo if hi > lo else 1.0
    idx = np.clip(((np.nan_to_num(f.grid, nan=lo) - lo) / span * 255).astype(int), 0, 255)
    rgba = np.zeros(f.grid.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = lut[idx][..., :3]
    rgba[..., 3] = np.where(np.isfinite(f.grid), OVERLAY_ALPHA, 0)
    dx, dy = f.xs[1] - f.xs[0], f.ys[1] - f.ys[0]
    return rgba, QtCore.QRectF(f.xs[0] - dx / 2, f.ys[0] - dy / 2, dx * f.xs.size, dy * f.ys.size)


@dataclass
class DicData:
    """One test as the DIC tab sees it (read on a worker thread)."""

    res: RunResults
    dic: dict = field(default_factory=dict)          # frame -> displacement CSV
    strain: dict = field(default_factory=dict)       # frame -> strain CSV
    strain_cols: list = field(default_factory=list)
    lengths: dict = field(default_factory=dict)      # tracker -> (frames, crack length px)
    manual: dict = field(default_factory=dict)       # frame -> (x, y), marked by hand
    speed: float | None = None
    files: dict = field(default_factory=dict)        # frame -> image file name
    time: dict = field(default_factory=dict)         # frame -> time_s
    position: dict = field(default_factory=dict)     # frame -> position in res.frames

    @classmethod
    def read(cls, folder: Path) -> "DicData":
        res = load_results(folder)
        data = cls(res)
        run = res.run
        if run is None:
            return data
        data.dic, data.strain = dr.dic_files(run), dr.strain_files(run)
        if data.strain:
            data.strain_cols = dr.strain_columns(next(iter(data.strain.values())))
        data.lengths = dr.tracker_lengths(run)
        data.manual = dr.read_manual(run)
        data.speed = dr.peak_crack_speed(run)
        fr = res.frames
        if fr is not None:
            for k, idx in enumerate(fr.index):
                i = int(idx)
                data.files[i] = fr.files[k]
                data.time[i] = float(fr.time_s[k])
                data.position[i] = k
        return data

    def window_frames(self) -> set[int]:
        out = set()
        for a, b in self.res.windows:
            out.update(i for i in self.files if a <= i <= b)
        return out


# ------------------------------------------------------------------ panes --

class FramePane(QtWidgets.QWidget):
    """A frame with an overlay and markers, a slider over a list of frames, and a plot against time."""

    overlay_label = ""

    def __init__(self, owner: "DicView", extra: list):
        super().__init__()
        self.owner = owner
        self.frames: list[int] = []              # camera frame numbers this pane steps through
        self.k = 0
        self._sized = None

        self.view = pg.GraphicsLayoutWidget()
        self.view.setBackground("k")
        self.view.setMinimumHeight(em(4))
        self.vb = self.view.addViewBox(row=0, col=0, lockAspect=True, invertY=True, enableMenu=False)
        self.image = pg.ImageItem(axisOrder="row-major")
        self.vb.addItem(self.image)
        self.overlay = pg.ImageItem(axisOrder="row-major")
        self.overlay.setZValue(5)
        self.vb.addItem(self.overlay)
        self.roi_dots = pg.ScatterPlotItem(size=2, pen=None, brush=pg.mkBrush(0, 200, 255, 140))
        self.notch = pg.ScatterPlotItem(symbol="t1", size=16, pen=pg.mkPen("#ff4df0", width=2),
                                        brush=pg.mkBrush(255, 77, 240, 100))
        self.markers = {name: pg.ScatterPlotItem(symbol=sym, size=18, pen=pg.mkPen(color, width=3), brush=None)
                        for name, (sym, color) in TIP_STYLES.items()}
        for item in (self.roi_dots, self.notch, *self.markers.values()):
            item.setZValue(10)
            self.vb.addItem(item)
        self.placeholder = pg.TextItem("", color="#dddddd", anchor=(0.5, 0.5))
        self.vb.addItem(self.placeholder)
        self.bar = None
        try:
            self.bar = pg.ColorBarItem(values=(0, 1), colorMap=colormap("CET-R4"), interactive=False,
                                       width=max(10, em(0.8)))
            self.view.addItem(self.bar, row=0, col=1)
            self.bar.setVisible(False)
        except Exception:  # noqa: BLE001 - older pyqtgraph: the range is in the info line anyway
            self.bar = None

        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._slid)
        self.play_btn = button("▶ Play", self.toggle_play)
        self.play_btn.setCheckable(True)
        nav = [button("◀◀", lambda: self.step(-10)), button("◀", lambda: self.step(-1)), self.play_btn,
               button("▶", lambda: self.step(1)), button("▶▶", lambda: self.step(10))]
        for b in nav:
            if b is not self.play_btn:
                b.setFixedWidth(em(3.2))
        self.nav = nav
        self.speed = QtWidgets.QComboBox()
        for fps in PLAY_FPS:
            self.speed.addItem(f"{fps} fps", fps)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(lambda _i: self._restart_timer())
        controls = flow(*nav, self.speed, *extra)

        self.info = QtWidgets.QLabel()
        self.info.setStyleSheet(status_style())
        self.info.setWordWrap(True)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)

        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setMinimumHeight(em(7))
        self.plot.setMaximumHeight(em(12))
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setLabel("bottom", "Time (s)")
        self.plot.addLegend(offset=(5, 5))
        self.now_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#1f5fa8", width=2))
        self.plot.addItem(self.now_line)
        self.plot.scene().sigMouseClicked.connect(self._plot_clicked)
        tick = QtGui.QFont()
        tick.setPixelSize(max(11, em(0.8)))
        for side in ("left", "bottom"):
            self.plot.getAxis(side).setStyle(tickFont=tick)
            self.plot.getAxis(side).enableAutoSIPrefix(False)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._play_step)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view, 3)
        layout.addWidget(self.slider)
        layout.addLayout(controls)
        layout.addWidget(self.info)
        layout.addWidget(self.note)
        layout.addWidget(self.plot, 1)

    # ---- what a pane provides
    def frame_list(self, data: DicData) -> list[int]:
        return []

    def draw_overlay(self, frame: int) -> str:
        """Draw the overlay for this frame; return a text for the info line."""
        self.overlay.setVisible(False)
        return ""

    def draw_plot(self) -> None:
        pass

    def empty_text(self, data: DicData) -> str:
        return "Nothing to show yet: run it from the Run tab."

    # ---- common
    @property
    def data(self) -> DicData | None:
        return self.owner.data

    def reset(self) -> None:
        """A test was (re)loaded."""
        self.stop_play()
        data = self.data
        keep = self.frames[self.k] if self.frames and self.k < len(self.frames) else None
        self.frames = self.frame_list(data) if data is not None and data.res.has_images else []
        self._sized = None
        for w in (self.slider, self.speed, *self.nav):
            w.setEnabled(bool(self.frames))
        self._draw_roi()
        self.draw_plot()
        if not self.frames:
            self.image.clear()
            self.overlay.setVisible(False)
            for item in self.markers.values():
                item.setData([], [])
            self.info.setText("")
            self._placeholder("No test selected" if data is None else
                              "The camera images were deleted" if data.res.images_deleted else self.empty_text(data))
            return
        self._placeholder("")
        self.k = int(np.searchsorted(self.frames, keep)) if keep is not None else self.start_position()
        self.k = min(max(self.k, 0), len(self.frames) - 1)
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(self.frames) - 1)
        self.slider.setPageStep(max(1, len(self.frames) // 20))
        self.slider.setValue(self.k)
        self.slider.blockSignals(False)
        self.redraw()

    def start_position(self) -> int:
        crack = self.data.res.crack
        if crack is not None and crack.first < len(self.data.res.frames):
            first = int(self.data.res.frames.index[min(crack.first, len(self.data.res.frames) - 1)])
            return int(np.searchsorted(self.frames, first))
        return 0

    def _placeholder(self, text: str) -> None:
        self.placeholder.setText(text)
        self.placeholder.setVisible(bool(text))
        if text:
            self.placeholder.setPos(320, 80)
            self.vb.setRange(QtCore.QRectF(0, 0, 640, 160), padding=0.02)
            if self.bar is not None:
                self.bar.setVisible(False)

    def _draw_roi(self) -> None:
        roi = self.data.res.roi if self.data is not None else None
        if roi is not None and roi.mask.any():
            xs, ys = outline(roi.mask, step=2)
            self.roi_dots.setData(xs + 0.5, ys + 0.5)
        else:
            self.roi_dots.setData([], [])
        if roi is not None and roi.notch:
            self.notch.setData([roi.notch[0] + 0.5], [roi.notch[1] + 0.5])
        else:
            self.notch.setData([], [])

    def _slid(self, value: int) -> None:
        self.k = value
        self.redraw()

    def go_frame(self, frame: int) -> None:
        if self.frames:
            self.go(int(np.argmin(np.abs(np.asarray(self.frames) - frame))))

    def go(self, k: int) -> None:
        if not self.frames:
            return
        self.k = min(max(int(k), 0), len(self.frames) - 1)
        self.slider.setValue(self.k)
        self.redraw()

    def step(self, n: int) -> None:
        self.go(self.k + n)

    @property
    def frame(self) -> int | None:
        return self.frames[self.k] if self.frames else None

    def redraw(self) -> None:
        frame = self.frame
        if frame is None or self.data is None or not self.owner.available:
            return
        image = self.owner.read_image(frame)
        if image is None:
            self.image.clear()
        else:
            self.image.setImage(image, autoLevels=False, levels=(0, 255))
            if image.shape != self._sized:
                self._sized = image.shape
                self.image.setRect(QtCore.QRectF(0, 0, image.shape[1], image.shape[0]))
                self.vb.setRange(QtCore.QRectF(0, 0, image.shape[1], image.shape[0]), padding=0.01)
        extra = self.draw_overlay(frame)
        tips = self.draw_markers(frame)
        t = self.data.time.get(frame, math.nan)
        if math.isfinite(t):
            self.now_line.setValue(t)
        fr = self.data.res.frames
        pos = self.data.position.get(frame)
        u = self.owner.units
        parts = [f"frame {frame} ({self.k + 1}/{len(self.frames)})", f"t {t:.4f} s" if math.isfinite(t) else "t —"]
        if pos is not None:
            parts += [f"load {u.format_force(fr.load_n[pos])}", f"ext {u.format_length(fr.extension_mm[pos])}"]
        if extra:
            parts.append(extra)
        if tips:
            parts.append(tips)
        self.info.setText("  ·  ".join(parts))

    def draw_markers(self, frame: int) -> str:
        for item in self.markers.values():
            item.setData([], [])
        return ""

    def show_field(self, f: dr.Field | None, lo: float, hi: float, cmap) -> bool:
        overlay = grid_overlay(f, lo, hi, cmap)
        if overlay is None:
            self.overlay.setVisible(False)
            if self.bar is not None:
                self.bar.setVisible(False)
            return False
        self.overlay.setImage(overlay[0], autoLevels=False)
        self.overlay.setRect(overlay[1])
        self.overlay.setVisible(True)
        if self.bar is not None:
            try:
                self.bar.setColorMap(cmap)
                self.bar.setLevels(values=(lo, hi))
                self.bar.setVisible(True)
            except Exception:  # noqa: BLE001
                self.bar.setVisible(False)
        return True

    def frame_at_time(self, t: float) -> int | None:
        times = np.array([self.data.time.get(f, np.nan) for f in self.frames])
        ok = np.isfinite(times)
        if not ok.any():
            return None
        return int(np.nanargmin(np.where(ok, np.abs(times - t), np.nan)))

    def _plot_clicked(self, event) -> None:
        if not self.frames:
            return
        vb = self.plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        k = self.frame_at_time(vb.mapSceneToView(event.scenePos()).x())
        if k is not None:
            self.go(k)

    def times_of(self, frames) -> np.ndarray:
        return np.array([self.data.time.get(int(f), np.nan) for f in frames], dtype=float)

    # ---- playing
    def toggle_play(self) -> None:
        if self.play_btn.isChecked() and self.frames:
            if self.k >= len(self.frames) - 1:
                self.go(0)
            self.play_btn.setText("❚❚ Pause")
            self._restart_timer()
        else:
            self.stop_play()

    def _restart_timer(self) -> None:
        if self.play_btn.isChecked():
            self.timer.start(int(1000 / float(self.speed.currentData() or 15)))

    def stop_play(self) -> None:
        self.timer.stop()
        self.play_btn.blockSignals(True)
        self.play_btn.setChecked(False)
        self.play_btn.blockSignals(False)
        self.play_btn.setText("▶ Play")

    def _play_step(self) -> None:
        if not self.frames or not self.owner.available or self.k >= len(self.frames) - 1:
            self.stop_play()
            return
        self.go(self.k + 1)


class DisplacementPane(FramePane):
    def __init__(self, owner):
        self.component = QtWidgets.QComboBox()
        for key, label in dr.DISPLACEMENTS.items():
            self.component.addItem(label, key)
        self.window_only = QtWidgets.QCheckBox("Full-rate window only")
        self.per_frame = QtWidgets.QCheckBox("Colours per frame")
        self.per_frame.setToolTip("Scale the colours to this frame instead of the whole test")
        super().__init__(owner, [self.component, self.window_only, self.per_frame])
        self.curve = self.plot.plot(pen=pg.mkPen("#1f5fa8", width=2), symbol="o", symbolSize=3, name="mean")
        self.limits: tuple[float, float] = (0.0, 1.0)
        self.component.currentIndexChanged.connect(lambda _i: self.recompute())
        self.window_only.toggled.connect(lambda _on: self.reset())
        self.per_frame.toggled.connect(lambda _on: self.redraw())

    def frame_list(self, data):
        frames = [f for f in data.dic if f in data.files]
        if self.window_only.isChecked():
            window = data.window_frames()
            frames = [f for f in frames if f in window] or frames
        return frames

    def empty_text(self, data):
        return "No DIC results yet: run DIC from the Run tab."

    def recompute(self) -> None:
        data, comp = self.data, self.component.currentData()
        if data is None or not data.dic:
            self.reset()
            return

        def work():
            files = list(data.dic.values())
            limits = dr.color_limits([dr.displacement_field(p, comp) for p in dr.sample(files)])
            series = dr.mean_series(data.dic, lambda p: dr.displacement_field(p, comp))
            return limits, series

        def done(result):
            if self.data is not data or self.component.currentData() != comp:
                return
            self.limits, (frames, means) = result
            self.curve.setData(self.times_of(frames), means, connect="finite")
            self.reset()

        self.note.setText("Reading the DIC results...")
        self.owner.win.run_async(work, done, lambda e: self.note.setText(f"Cannot read the DIC results: {e}"),
                                 busy=False)

    def draw_plot(self) -> None:
        label = dr.DISPLACEMENTS[self.component.currentData()]
        self.plot.setLabel("left", f"Mean {label.split(':')[0]} (px)")
        if self.data is None or not self.data.dic:
            self.curve.setData([], [])
            self.note.setText("")
        else:
            self.note.setText(f"{len(self.data.dic)} frames with DIC results. Colours: "
                              f"{self.limits[0]:.3f} to {self.limits[1]:.3f} px (2nd–98th percentile).")

    def draw_overlay(self, frame):
        comp = self.component.currentData()
        f = dr.displacement_field(self.data.dic[frame], comp)
        if f is None:
            self.show_field(None, 0, 1, None)
            return "cannot read the DIC file"
        lo, hi = dr.color_limits([f]) if self.per_frame.isChecked() else self.limits
        self.show_field(f, lo, hi, colormap("viridis" if comp == "mag" else "CET-R4"))
        vals = f.finite()
        mag = np.abs(vals) if vals.size else vals
        return (f"mean {np.mean(vals):.3f} px, max |{comp}| {np.max(mag):.3f} px, "
                f"{100 * f.valid:.0f} % converged" if vals.size else "no converged points")


class StrainPane(FramePane):
    def __init__(self, owner):
        self.component = QtWidgets.QComboBox()
        self.window_only = QtWidgets.QCheckBox("Full-rate window only")
        self.per_frame = QtWidgets.QCheckBox("Colours per frame")
        super().__init__(owner, [self.component, self.window_only, self.per_frame])
        self.curve = self.plot.plot(pen=pg.mkPen("#8e44ad", width=2), symbol="o", symbolSize=3, name="mean strain")
        # Load on a second axis.
        item = self.plot.getPlotItem()
        self.load_vb = pg.ViewBox()
        item.showAxis("right")
        item.scene().addItem(self.load_vb)
        item.getAxis("right").linkToView(self.load_vb)
        self.load_vb.setXLink(item)
        self.load_curve = pg.PlotCurveItem(pen=pg.mkPen("#c0392b", width=1.5))
        self.load_vb.addItem(self.load_curve)
        item.vb.sigResized.connect(lambda: self.load_vb.setGeometry(item.vb.sceneBoundingRect()))
        self.limits: tuple[float, float] = (0.0, 1e-3)
        self.component.currentIndexChanged.connect(lambda _i: self.recompute())
        self.window_only.toggled.connect(lambda _on: self.reset())
        self.per_frame.toggled.connect(lambda _on: self.redraw())

    def fill_components(self) -> None:
        data = self.data
        self.component.blockSignals(True)
        current = self.component.currentData()
        self.component.clear()
        for col in (data.strain_cols if data is not None else []):
            self.component.addItem(dr.STRAIN_NAMES.get(col, col), col)
        index = self.component.findData(current)
        self.component.setCurrentIndex(max(index, 0))
        self.component.blockSignals(False)

    def frame_list(self, data):
        frames = [f for f in data.strain if f in data.files]
        if self.window_only.isChecked():
            window = data.window_frames()
            frames = [f for f in frames if f in window] or frames
        return frames

    def empty_text(self, data):
        return "No strain results yet: run DIC and Strain from the Run tab."

    def recompute(self) -> None:
        data, col = self.data, self.component.currentData()
        if data is None or not data.strain or not col:
            self.reset()
            return

        def work():
            files = list(data.strain.values())
            limits = dr.color_limits([dr.strain_field(p, col) for p in dr.sample(files)])
            series = dr.mean_series(data.strain, lambda p: dr.strain_field(p, col))
            return limits, series

        def done(result):
            if self.data is not data or self.component.currentData() != col:
                return
            self.limits, (frames, means) = result
            self.curve.setData(self.times_of(frames), 1e6 * means, connect="finite")
            self.reset()

        self.note.setText("Reading the strain results...")
        self.owner.win.run_async(work, done, lambda e: self.note.setText(f"Cannot read the strain results: {e}"),
                                 busy=False)

    def draw_plot(self) -> None:
        u = self.owner.units
        col = self.component.currentData() or dr.DEFAULT_STRAIN
        self.plot.setLabel("left", f"Mean {dr.STRAIN_NAMES.get(col, col).split(' ')[0]} (µε)")
        self.plot.getPlotItem().setLabel("right", f"Load ({u.force})")
        data = self.data
        res = data.res if data is not None else None
        if res is not None and res.data.get("time_s") is not None and res.data["time_s"].size:
            self.load_curve.setData(res.data["time_s"], u.force_from_n(np.nan_to_num(res.data["load_N"])))
        else:
            self.load_curve.setData([], [])
        if data is None or not data.strain:
            self.curve.setData([], [])
            self.note.setText("")
            return
        note = (f"{len(data.strain)} frames with strain. Colours: {1e6 * self.limits[0]:.0f} to "
                f"{1e6 * self.limits[1]:.0f} µε (2nd–98th percentile).")
        if data.dic and len(data.strain) > len(data.dic):
            note += " ⚠ More strain files than DIC results: files from an earlier run are mixed in."
        self.note.setText(note)

    def draw_overlay(self, frame):
        col = self.component.currentData()
        f = dr.strain_field(self.data.strain[frame], col) if col else None
        if f is None:
            self.show_field(None, 0, 1, None)
            return "cannot read the strain file"
        lo, hi = dr.color_limits([f]) if self.per_frame.isChecked() else self.limits
        self.show_field(f, lo, hi, colormap("CET-R4"))
        vals = f.finite()
        return (f"mean {1e6 * np.mean(vals):.0f} µε, {1e6 * np.min(vals):.0f} to {1e6 * np.max(vals):.0f} µε"
                if vals.size else "no values")


class CrackPane(FramePane):
    def __init__(self, owner):
        self.all_frames = QtWidgets.QCheckBox("All frames")
        self.all_frames.setToolTip("Every saved frame, not only the full-rate window")
        self.strain_on = QtWidgets.QCheckBox("Strain overlay")
        self.delete_btn = button("Delete mark", self.delete_mark)
        self.zoom_btn = button("Zoom to tip", self.toggle_zoom)
        self.zoom_btn.setCheckable(True)
        super().__init__(owner, [self.all_frames, self.strain_on, self.delete_btn, self.zoom_btn])
        self.hint = QtWidgets.QLabel("Click the frame to mark the crack tip there (right-click removes it). Your marks "
                                     "are saved at once to camera/crack_tip_manual.csv, the file "
                                     "crack_tip_viewer.py uses.")
        self.hint.setWordWrap(True)
        self.layout().insertWidget(4, self.hint)
        self.plot.setLabel("left", "Crack length (px)")
        self.lines = {name: self.plot.plot(pen=pg.mkPen(color, width=2), name=name)
                      for name, (_sym, color) in TIP_STYLES.items() if name != "by hand"}
        self.manual_dots = self.plot.plot(pen=None, symbol="o", symbolSize=em(0.6), symbolBrush="#d4ac0d",
                                          symbolPen=None, name="by hand")
        self._strain_limits: tuple[float, float] | None = None
        self.vb.scene().sigMouseClicked.connect(self._image_clicked)
        self.all_frames.toggled.connect(lambda _on: self.reset())
        self.strain_on.toggled.connect(lambda _on: self.redraw())

    def frame_list(self, data):
        if self.all_frames.isChecked():
            return sorted(data.files)
        window = data.window_frames()
        return sorted(window) if window else sorted(data.files)

    def empty_text(self, data):
        return "No saved frames."

    def reset(self) -> None:
        self._strain_limits = None
        self.strain_on.setEnabled(self.data is not None and bool(self.data.strain))
        super().reset()

    def _notch(self):
        roi = self.data.res.roi if self.data is not None else None
        return (roi.notch, roi.crack_from_left) if roi is not None else (None, True)

    def draw_plot(self) -> None:
        data = self.data
        if data is None:
            for line in self.lines.values():
                line.setData([], [])
            self.manual_dots.setData([], [])
            self.note.setText("")
            return
        for name, line in self.lines.items():
            frames, lengths = data.lengths.get(name, (np.zeros(0), np.zeros(0)))
            line.setData(self.times_of(frames), lengths, connect="finite")
        notch, left = self._notch()
        frames = sorted(data.manual)
        self.manual_dots.setData(self.times_of(frames), [dr.crack_length(data.manual[f][0], notch, left)
                                                         for f in frames])
        self.note.setText(self._summary())

    def _summary(self) -> str:
        data = self.data
        parts = []
        for name in ("image tracker", "opening method"):
            if name in data.res.tips:
                parts.append(f"{name}: tip in {len(data.res.tips[name])} frames")
        if data.speed is not None:
            parts.append(f"peak crack speed {data.speed:.0f} px/s (opening method)")
        if data.manual:
            parts.append(f"{len(data.manual)} marked by hand")
            for name in ("image tracker", "opening method"):
                tips = data.res.tips.get(name, {})
                common = [f for f in data.manual if f in tips]
                if common:
                    d = np.mean([math.hypot(tips[f][0] - data.manual[f][0], tips[f][1] - data.manual[f][1])
                                 for f in common])
                    parts.append(f"{name} {d:.1f} px from your marks ({len(common)} frames)")
        if not parts:
            return "No crack tips yet: run the crack trackers from the Run tab, or mark tips here by hand."
        return "; ".join(parts) + "."

    def draw_overlay(self, frame):
        data = self.data
        if self.strain_on.isChecked() and frame in data.strain:
            col = dr.DEFAULT_STRAIN if dr.DEFAULT_STRAIN in data.strain_cols else (data.strain_cols or [None])[0]
            if self._strain_limits is None and col:
                files = [data.strain[f] for f in self.frames if f in data.strain]
                self._strain_limits = dr.color_limits([dr.strain_field(p, col) for p in dr.sample(files, 20)])
            if col and self.show_field(dr.strain_field(data.strain[frame], col), *self._strain_limits,
                                       colormap("CET-R4")):
                return f"strain overlay {dr.STRAIN_NAMES.get(col, col)}"
        self.show_field(None, 0, 1, None)
        return ""

    def draw_markers(self, frame):
        data = self.data
        tips = dict(data.res.tips_at(data.position[frame])) if frame in data.position else {}
        tips.pop("by hand", None)
        if frame in data.manual:
            tips["by hand"] = data.manual[frame]
        notch, left = self._notch()
        for name, item in self.markers.items():
            if name in tips:
                item.setData([tips[name][0] + 0.5], [tips[name][1] + 0.5])
            else:
                item.setData([], [])
        self.delete_btn.setEnabled(frame in data.manual)
        if self.zoom_btn.isChecked():
            self._zoom(tips)
        return ", ".join(f"{name} {dr.crack_length(xy[0], notch, left):.0f} px" for name, xy in tips.items())

    def _zoom(self, tips: dict) -> None:
        if self._sized is None:
            return
        h, w = self._sized
        centre = next((tips[n] for n in ("by hand", "image tracker", "opening method") if n in tips), None)
        notch, _left = self._notch()
        centre = centre or notch or (w / 2, h / 2)
        self.vb.setRange(QtCore.QRectF(centre[0] - w / 4, centre[1] - h / 4, w / 2, h / 2), padding=0)

    def toggle_zoom(self) -> None:
        if self.zoom_btn.isChecked():
            self.redraw()
        elif self._sized is not None:
            h, w = self._sized
            self.vb.setRange(QtCore.QRectF(0, 0, w, h), padding=0.01)

    def _image_clicked(self, event) -> None:
        frame = self.frame
        if frame is None or self._sized is None or not self.owner.available:
            return
        if not self.vb.sceneBoundingRect().contains(event.scenePos()):
            return
        pos = self.vb.mapSceneToView(event.scenePos())
        h, w = self._sized
        if not (0 <= pos.x() < w and 0 <= pos.y() < h):
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.delete_mark()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.mark(frame, float(pos.x()), float(pos.y()))

    def mark(self, frame: int, x: float, y: float) -> None:
        self.data.manual[frame] = (x, y)
        self._save()

    def delete_mark(self) -> None:
        frame = self.frame
        if frame is not None and self.data is not None and frame in self.data.manual:
            del self.data.manual[frame]
            self._save()

    def _save(self) -> None:
        data = self.data
        try:
            dr.write_manual(data.res.run, data.manual, data.time)
        except OSError as e:
            self.owner.win.warn("Crack tip", f"Could not save the marks: {e}")
        self.draw_plot()
        self.redraw()


class RunPane(QtWidgets.QWidget):
    """What to run, the checklist, Run / Cancel and the progress."""

    def __init__(self, owner: "DicView"):
        super().__init__()
        self.owner = owner
        self.checks: list[dr.Check] = []
        self.run_dic = QtWidgets.QCheckBox("DIC (capture_dic_live.py's dic_worker)")
        self.run_strain = QtWidgets.QCheckBox("Strain + plots (pyvale_dic_run.py)")
        self.crack_image = QtWidgets.QCheckBox("Crack tip: image tracker (crack_tip_image.py)")
        self.crack_opening = QtWidgets.QCheckBox("Crack tip: opening method (crack_tip_opening.py)")
        self.redo = QtWidgets.QCheckBox("Redo frames that already have DIC results")
        self.overview_every = spin(1, 1000, 3, 0, 1)
        self.overview_every.setToolTip("DIC on every Nth saved frame outside the full-rate windows")
        self.crack_every = spin(1, 1000, 1, 0, 1)
        self.crack_every.setToolTip("DIC on every Nth frame of the full-rate windows (the break)")
        self.subset_size = spin(5, 501, 41, 0, 2)
        self.subset_step = spin(1, 500, 20, 0, 1)
        self.max_disp = spin(1, 2000, 64, 0, 8)
        self._defaults()

        what = QtWidgets.QGroupBox("What to run")
        wl = QtWidgets.QVBoxLayout(what)
        for box in (self.run_dic, self.run_strain, self.crack_image, self.crack_opening, self.redo):
            wl.addWidget(box)
        params = QtWidgets.QGroupBox("DIC parameters")
        pf = form_layout(params)
        pf.addRow("Whole test: every Nth saved frame", self.overview_every)
        pf.addRow("Full-rate windows: every Nth frame", self.crack_every)
        pf.addRow("Subset size (px)", self.subset_size)
        pf.addRow("Subset step (px)", self.subset_step)
        pf.addRow("Max displacement (px)", self.max_disp)
        self.defaults_btn = button("Camera panel's values", self._defaults)
        pf.addRow(self.defaults_btn)

        check_box = QtWidgets.QGroupBox("Checks")
        self.check_text = QtWidgets.QLabel()
        self.check_text.setWordWrap(True)
        self.check_text.setTextFormat(Qt.TextFormat.RichText)
        self.fix_btn = button("Use the current ROI", self.use_current_roi)
        self.fix_btn.hide()
        self.recheck_btn = button("Check again", self.owner_recheck)
        cl = QtWidgets.QVBoxLayout(check_box)
        cl.addWidget(self.check_text)
        cl.addLayout(flow(self.fix_btn, self.recheck_btn))

        self.run_btn = button("▶  Run", self.run)
        self.run_btn.setMinimumHeight(em(2.8))
        self.cancel_btn = button("Cancel", self.cancel)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_text = QtWidgets.QLabel()
        self.progress_text.setWordWrap(True)
        self.progress_text.setStyleSheet(status_style())

        for w in (self.run_dic, self.run_strain, self.crack_image, self.crack_opening, self.redo):
            w.toggled.connect(lambda _on: self.validate())
        for w in (self.overview_every, self.crack_every):
            w.valueChanged.connect(lambda _v: self.validate())

        inner = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(inner)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(what, 1)
        top.addWidget(params, 1)
        layout.addLayout(top)
        layout.addWidget(check_box)
        layout.addLayout(flow(self.run_btn, self.cancel_btn))
        layout.addWidget(self.progress)
        layout.addWidget(self.progress_text)
        layout.addStretch(1)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        area.setWidget(inner)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

    def owner_recheck(self) -> None:
        global _env_check
        _env_check = None
        self.owner.check_environment()
        self.validate()

    def _defaults(self) -> None:
        """The Camera panel's choices (or the config and capture_dic_live.py's settings without a camera)."""
        win = self.owner.win
        panel = getattr(win, "camera_panel", None)
        if panel is not None:
            o = panel.post_options()
        else:
            o = Options.from_config(win.config)
            s = literal_settings(win.config.camera_dir)
            o.overview_every = max(1, int(s.get("DIC_STEP") or 27) // max(1, int(s.get("SAVE_EVERY") or 9)))
            o.crack_every = int(s.get("CRACK_DIC_STEP") or 1)
            o.subset_size, o.subset_step = int(s.get("subset_size") or 41), int(s.get("subset_step") or 20)
            o.max_displacement = int(s.get("max_displacement") or 64)
        self.run_dic.setChecked(o.run_dic)
        self.run_strain.setChecked(o.run_strain)
        self.crack_image.setChecked(o.crack_tip_image)
        self.crack_opening.setChecked(o.crack_tip_opening)
        for box, value in ((self.overview_every, o.overview_every), (self.crack_every, o.crack_every),
                           (self.subset_size, o.subset_size), (self.subset_step, o.subset_step),
                           (self.max_disp, o.max_displacement)):
            if value:
                box.setValue(value)

    def options(self) -> Options:
        return Options(run_dic=self.run_dic.isChecked(), run_strain=self.run_strain.isChecked(),
                       strain_animation=False, crack_tip_image=self.crack_image.isChecked(),
                       crack_tip_opening=self.crack_opening.isChecked(), make_video=False,
                       overview_every=int(self.overview_every.value()), crack_every=int(self.crack_every.value()),
                       subset_size=int(self.subset_size.value()), subset_step=int(self.subset_step.value()),
                       max_displacement=int(self.max_disp.value()), redo=self.redo.isChecked())

    def _current_roi(self):
        win = self.owner.win
        roi = getattr(win, "roi", None)
        return roi if roi is not None else load_roi(win.roi_file)

    def validate(self) -> None:
        data = self.owner.data
        if data is None:
            self.checks = []
            self.check_text.setText("Choose a test above.")
            self.fix_btn.hide()
            self._enable()
            return
        win = self.owner.win
        try:
            disk = disk_status(win.config.output_dir, win.config.storage.warn_free_gb,
                               win.config.storage.critical_free_gb)
        except OSError:
            disk = None
        run = data.res.run
        self.checks = dr.validate(data.res, self.options(), current_roi=self._current_roi(), env=_env_check,
                                  scripts=check_scripts(win.config.camera_dir), disk=disk,
                                  busy=run is not None and win.postprocess.running_for(run))
        lines = [f"<span style='color:{LEVEL_COLOR[c.level]}'><b>{c.mark}</b></span> {_html(c.text)}"
                 for c in self.checks]
        self.check_text.setText("<br>".join(lines))
        self.fix_btn.setVisible(any(c.fix == "use_current_roi" for c in self.checks))
        notch = any(c.text.startswith("Notch tip at") for c in self.checks)
        for box in (self.crack_image, self.crack_opening):
            box.setEnabled(notch)
            if not notch:
                box.setToolTip("Needs a notch tip: Camera tab > Edit ROI > Place notch tip")
        self._enable()

    def _enable(self) -> None:
        data = self.owner.data
        run = data.res.run if data is not None else None
        busy = run is not None and self.owner.win.postprocess.running_for(run)
        self.run_btn.setEnabled(self.owner.available and bool(self.checks) and dr.can_run(self.checks))
        self.cancel_btn.setEnabled(busy)

    def use_current_roi(self) -> None:
        data = self.owner.data
        roi = self._current_roi()
        if data is None or data.res.run is None or roi is None:
            return
        try:
            save_roi(data.res.run / "roi.npz", roi)
        except OSError as e:
            self.owner.win.warn("DIC", f"Could not save the ROI: {e}")
            return
        self.owner.win.status("Saved the current ROI with this recording")
        self.owner.reload()

    def run(self) -> None:
        data = self.owner.data
        self.validate()
        if data is None or not dr.can_run(self.checks):
            return
        options = self.options()
        notch = any(c.text.startswith("Notch tip at") for c in self.checks)
        if not notch:
            options.crack_tip_image = options.crack_tip_opening = False
        self.owner.win.queue_postprocess(data.res.run, options=options)
        self.owner.win.status(f"DIC queued for {data.res.name}")
        self.progress_text.setText("Queued...")

    def cancel(self) -> None:
        data = self.owner.data
        if data is not None and data.res.run is not None and self.owner.win.ask(
                "DIC", "Stop this test's DIC jobs? Frames already analysed keep their results."):
            self.owner.win.postprocess.cancel_run(data.res.run)
            self.progress_text.setText("Cancelled.")
            self.validate()

    def show_progress(self) -> None:
        data = self.owner.data
        pp = self.owner.win.postprocess
        run = data.res.run if data is not None else None
        if run is None:
            self.progress.setValue(0)
            return
        current = pp.current if pp.current is not None and Path(pp.current.run) == run else None
        queued = sum(1 for j in pp.queue if Path(j.run) == run)
        if current is not None:
            frac = pp.progress if isinstance(pp.progress, (int, float)) else 0.0
            self.progress.setValue(int(1000 * frac))
            text = pp.text + (f" · {queued} more job(s) queued" if queued else "")
            if pp.paused:
                text += " · paused during the test"
            self.progress_text.setText(text)
        elif queued:
            self.progress.setValue(0)
            self.progress_text.setText(f"{queued} job(s) queued behind another test's post-processing.")
        else:
            done = [j for j in pp.done if Path(j.run) == run]
            if done:
                self.progress.setValue(1000 if all(j.ok for j in done[-6:]) else 0)
                self.progress_text.setText("Last run: " + "; ".join(f"{j.title.split(': ', 1)[-1]}: {j.result}"
                                                                    for j in done[-6:]))
        self._enable()


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------- tab --

class DicView(QtWidgets.QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.units: Units = win.units
        self.data: DicData | None = None
        self.available = True
        self._stale = True
        self._listing = False
        self._pending: str | None = None
        self._generation = 0
        self._was_busy = False
        self._cache: OrderedDict = OrderedDict()

        self.choice = QtWidgets.QComboBox()
        self.choice.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.choice.setMinimumContentsLength(18)
        self.choice.activated.connect(lambda _i: self._choose())
        self.reload_btn = button("⟳", self.reload)
        self.reload_btn.setFixedWidth(em(3))
        self.reload_btn.setToolTip("Read the list of tests and this test's results again")
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Test:"))
        top.addWidget(self.choice, 1)
        top.addWidget(self.reload_btn)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(status_style())

        self.run_pane = RunPane(self)
        self.dic_pane = DisplacementPane(self)
        self.strain_pane = StrainPane(self)
        self.crack_pane = CrackPane(self)
        self.log_text = QtWidgets.QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(mono_font())
        self.panes = (self.dic_pane, self.strain_pane, self.crack_pane)
        self.inner = FlowTabs()
        self.inner.addTab(self.run_pane, "Run")
        self.inner.addTab(self.dic_pane, "DIC")
        self.inner.addTab(self.strain_pane, "Strain")
        self.inner.addTab(self.crack_pane, "Crack tip")
        self.inner.addTab(self.log_text, "Log")
        self.inner.currentChanged.connect(self._inner_changed)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.inner, 1)
        self._show(None)

    # ---------------------------------------------------------- availability --
    def set_available(self, on: bool) -> None:
        if on == self.available:
            return
        self.available = on
        if not on:
            for pane in self.panes:
                pane.stop_play()
            self._cache.clear()
            self._stale = True
        self.run_pane._enable()

    def on_shown(self) -> None:
        if not self.available:
            return
        self.check_environment()
        if self._stale:
            self.refresh_list()
        elif self.data is not None:
            self.load(self.data.res.folder)

    def mark_stale(self, newest: Path | None = None) -> None:
        self._stale = True
        if newest is not None:
            self._pending = str(newest)

    def check_environment(self) -> None:
        """Does the camera environment have pyvale? Once per session, on a worker thread."""
        if _env_check is not None or getattr(self, "_env_running", False):
            return
        from vsbc.camera.client import camera_env, camera_python

        self._env_running = True
        python = camera_python(self.win.config)

        def done(result):
            global _env_check
            self._env_running = False
            _env_check = result
            self.run_pane.validate()

        def failed(exc):
            global _env_check
            self._env_running = False
            _env_check = (False, str(exc))
            self.run_pane.validate()

        self.win.run_async(lambda: dr.check_camera_env(python, camera_env()), done, failed, busy=False)

    # --------------------------------------------------------------- listing --
    def refresh_list(self, then_show: str | None = None) -> None:
        if then_show is not None:
            self._pending = then_show
        if self._listing:
            return
        self._listing = True
        output_dir = self.win.config.output_dir

        def work():
            return [e for e in list_outputs(output_dir) if (e.camera_dir / "frames.csv").is_file()]

        def done(entries):
            self._listing = False
            self._stale = False
            self._fill(entries)

        def failed(exc):
            self._listing = False
            self.status.setText(f"Could not list the tests: {exc}")

        self.win.run_async(work, done, failed, busy=False)

    def _fill(self, entries) -> None:
        current = self._pending or (str(self.data.res.folder) if self.data is not None else None)
        self._pending = None
        self.choice.blockSignals(True)
        self.choice.clear()
        for e in entries:
            try:
                started = datetime.fromisoformat(e.started).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                started = e.started
            dic = f"DIC {e.n_dic} frames" if e.n_dic else "no DIC yet"
            gone = ", images deleted" if e.images_deleted else ""
            kind = "" if e.kind == "test" else "camera only, "
            self.choice.addItem(f"{started} · {e.name} ({kind}{dic}{gone})", str(e.path))
        self.choice.blockSignals(False)
        if not entries:
            self._show(None)
            self.status.setText("No tests with camera frames yet.")
            return
        index = self.choice.findData(current) if current else -1
        self.choice.setCurrentIndex(max(index, 0))
        if self.data is None or str(self.data.res.folder) != self.choice.currentData():
            self._choose()

    def _choose(self) -> None:
        folder = self.choice.currentData()
        if folder:
            self.load(Path(folder))

    def reload(self) -> None:
        self._stale = True
        self.refresh_list()
        if self.data is not None:
            self.load(self.data.res.folder)

    # --------------------------------------------------------------- loading --
    def load(self, folder: Path) -> None:
        self._generation += 1
        gen = self._generation
        self.status.setText(f"Reading {Path(folder).name}...")

        def done(data):
            if gen == self._generation:
                self._show(data)

        def failed(exc):
            if gen == self._generation:
                self._show(None)
                self.status.setText(f"Cannot read {Path(folder).name}: {exc}")

        self.win.run_async(lambda: DicData.read(Path(folder)), done, failed, busy=False)

    def _show(self, data: DicData | None) -> None:
        if data is None or self.data is None or data.res.folder != self.data.res.folder:
            self._cache.clear()
        self.data = data
        run = data.res.run if data is not None else None
        self._was_busy = run is not None and self.win.postprocess.running_for(run)
        self.status.setText(self._status_text())
        self.strain_pane.fill_components()
        for pane in self.panes:
            pane.reset()
        if data is not None:
            self.dic_pane.recompute()
            self.strain_pane.recompute()
        self.run_pane.validate()
        self.run_pane.show_progress()
        self._fill_log()

    def _status_text(self) -> str:
        data = self.data
        if data is None:
            return ""
        res = data.res
        parts = [str(res.run or res.folder)]
        if res.frames is not None:
            parts.append(f"{len(res.frames)} saved frames")
        parts.append(f"{len(data.dic)} with DIC" if data.dic else "no DIC results yet")
        if data.strain:
            parts.append(f"{len(data.strain)} with strain")
        return " · ".join(parts)

    def _fill_log(self) -> None:
        data = self.data
        if data is None or data.res.run is None:
            self.log_text.setPlainText("")
            return
        lines = []
        done = [j for j in self.win.postprocess.done if Path(j.run) == data.res.run]
        if done:
            lines.append("This session:")
            lines += [f"  {'✓' if j.ok else '✗'} {j.title}: {j.result}" for j in done]
            lines.append("")
        path = data.res.run / "postprocess.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
            lines.append(f"{path} (last {min(300, len(text))} lines):")
            lines += text[-300:]
        except OSError:
            lines.append("No post-processing log yet.")
        self.log_text.setPlainText("\n".join(lines))
        self.log_text.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _inner_changed(self, index: int) -> None:
        for pane in self.panes:
            if self.inner.widget(index) is not pane:
                pane.stop_play()
        if self.inner.widget(index) is self.log_text:
            self._fill_log()
        elif self.inner.widget(index) is self.run_pane:
            self.run_pane.validate()

    # ---------------------------------------------------------------- frames --
    def read_image(self, frame: int):
        data = self.data
        if data is None or frame not in data.files:
            return None
        key = (str(data.res.run), frame)
        image = self._cache.get(key)
        if image is not None:
            self._cache.move_to_end(key)
            return image
        try:
            image = tiff.read(data.res.images_dir / data.files[frame])
        except (OSError, ValueError, ImportError):
            return None
        self._cache[key] = image
        while len(self._cache) > CACHE_FRAMES:
            self._cache.popitem(last=False)
        return image

    # ------------------------------------------------------------------ misc --
    def refresh_status(self) -> None:
        """Slow GUI tick: progress, and a reload when this test's jobs finish."""
        data = self.data
        if data is None or data.res.run is None:
            return
        busy = self.win.postprocess.running_for(data.res.run)
        if self.inner.currentWidget() is self.run_pane:
            self.run_pane.show_progress()
        if self._was_busy and not busy:
            self._was_busy = False
            self.load(data.res.folder)              # new DIC, strain and crack-tip results
        elif busy != self._was_busy:
            self._was_busy = busy
            self.run_pane.validate()

    def apply_units(self, units: Units) -> None:
        self.units = units
        if self.data is not None:
            for pane in self.panes:
                pane.draw_plot()
                pane.redraw()
