"""The Results tab (left side): a finished test's camera frames, load curve,
plots and summary, without leaving the app.

Frames: scrub or play the saved frames with the load-time curve below, the
ROI, notch and crack-tip markers on top, and the DIC map where pyvale wrote
one. The crack frames come from the break load (vsbc.camera.results): the
frames between the last load reading before the drop and the first after it,
shaded on the curve and tagged CRACK, with the largest image change marked
as the estimated crack frame.

Not available while a test (or any camera recording) runs: reading frames
would compete with the camera for the disk. Frames are read on demand into a
small cache, so a long test does not fill the Pi's RAM.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from vsbc.camera import tiff
from vsbc.camera.results import RunResults, estimate_crack_frame, load_results
from vsbc.camera.roi import outline
from vsbc.gui.camera_view import dic_overlay
from vsbc.gui.common import (
    NOTCH_COLOR, PLACEHOLDER_COLOR, TIP_STYLES, FlowTabs, Qt, QtCore, QtGui, QtWidgets, button, em, flow,
    mono_font, np, pg, status_style, style_plot, theme, themed_pen,
)
from vsbc.storage import list_outputs
from vsbc.units import Units

log = logging.getLogger(__name__)

CACHE_FRAMES = 64              # 0.77 MB each at 1920 × 400: about 50 MB
PLAY_FPS = (5, 15, 30)
CRACK_BRUSH = (192, 57, 43, 60)
FULL_RATE_BRUSH = (243, 156, 18, 35)


class ScaledImage(QtWidgets.QLabel):
    """A picture scaled to fit, keeping its aspect ratio."""

    def __init__(self):
        super().__init__()
        self._pixmap: QtGui.QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(em(8), em(6))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)

    def set_file(self, path: Path | None) -> None:
        self._pixmap = QtGui.QPixmap(str(path)) if path is not None else None
        if self._pixmap is not None and self._pixmap.isNull():
            self._pixmap = None
            self.setText("Cannot show this image.")
        elif self._pixmap is None:
            self.setText("No plots yet.")
        self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is not None:
            self.setPixmap(self._pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))


class ResultsView(QtWidgets.QWidget):
    """Results > Overview. Its test picker (`top_bar`) is shared with DIC analysis (ResultsTab)."""

    def __init__(self, win, own_bar: bool = True):
        super().__init__()
        self.win = win
        self.on_chosen = None              # called with each folder chosen in the picker (ResultsTab)
        self.units: Units = win.units
        self.res: RunResults | None = None
        self.pos = 0                       # position in res.frames
        self.available = True
        self._stale = True                 # the list of tests needs reading again
        self._listing = False
        self._loading: str | None = None
        self._pending: str | None = None   # a folder to show once the list is in
        self._generation = 0
        self._cache: OrderedDict = OrderedDict()
        self._shown_size: tuple[int, int] | None = None

        # ---- top bar
        self.choice = QtWidgets.QComboBox()
        self.choice.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.choice.setMinimumContentsLength(18)
        self.choice.activated.connect(lambda _i: self._choose())
        self.reload_btn = button("⟳", self.reload)
        self.reload_btn.setFixedWidth(em(3))
        self.reload_btn.setToolTip("Read the list of tests and this test's files again (new DIC results, plots)")
        self.folder_btn = button("Open folder", self.open_folder, icon_name="folder")
        self.video_btn = button("Play video", self.play_video, icon_name="video")
        self.video_btn.setToolTip("video.mp4 in the system's video player")
        self.top_bar = QtWidgets.QWidget()
        top = QtWidgets.QHBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        test_label = QtWidgets.QLabel("Test:")
        test_label.setStyleSheet("QLabel { font-weight: bold; }")
        top.addWidget(test_label)
        top.addWidget(self.choice, 1)
        top.addWidget(self.reload_btn)
        top.addWidget(self.folder_btn)
        top.addWidget(self.video_btn)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(status_style())

        self.inner = FlowTabs()
        self.inner.addTab(self._build_frames(), "Frames")
        self.inner.addTab(self._build_plots(), "Plots")
        self.summary = QtWidgets.QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFont(mono_font())
        self.inner.addTab(self.summary, "Summary")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0) if not own_bar else layout.setContentsMargins(4, 4, 4, 4)
        if own_bar:
            layout.addWidget(self.top_bar)
        layout.addWidget(self.status)
        layout.addWidget(self.inner, 1)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._play_step)
        self._show(None)

    # ------------------------------------------------------------- building --
    def _build_frames(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.view = pg.GraphicsLayoutWidget()
        self.view.setBackground("k")
        self.view.setMinimumHeight(em(4))
        self.vb = self.view.addViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self.image = pg.ImageItem(axisOrder="row-major")
        self.vb.addItem(self.image)
        self.dic_item = pg.ImageItem(axisOrder="row-major")
        self.dic_item.setZValue(5)
        self.vb.addItem(self.dic_item)
        self.roi_dots = pg.ScatterPlotItem(size=2, pen=None, brush=pg.mkBrush(0, 200, 255, 160))
        self.notch_item = pg.ScatterPlotItem(symbol="t1", size=16, pen=pg.mkPen(NOTCH_COLOR, width=2),
                                             brush=pg.mkBrush(255, 77, 240, 100))
        self.tip_items = {name: pg.ScatterPlotItem(symbol=sym, size=18, pen=pg.mkPen(color, width=3), brush=None)
                          for name, (sym, color) in TIP_STYLES.items()}
        for item in (self.roi_dots, self.notch_item, *self.tip_items.values()):
            item.setZValue(10)
            self.vb.addItem(item)
        self.placeholder = pg.TextItem("", color=PLACEHOLDER_COLOR, anchor=(0.5, 0.5))
        self.vb.addItem(self.placeholder)

        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._slider_moved)
        self.back10 = button("◀◀", lambda: self.step(-10))
        self.back1 = button("◀", lambda: self.step(-1))
        self.play_btn = button("▶ Play", self.toggle_play)
        self.play_btn.setCheckable(True)
        self.fwd1 = button("▶", lambda: self.step(1))
        self.fwd10 = button("▶▶", lambda: self.step(10))
        self.speed = QtWidgets.QComboBox()
        for fps in PLAY_FPS:
            self.speed.addItem(f"{fps} fps", fps)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(lambda _i: self._restart_timer())
        self.crack_btn = button("Jump to crack", self.jump_to_crack)
        self.crack_only = QtWidgets.QCheckBox("Crack frames only")
        self.crack_only.setToolTip("Scrub and play only the crack window: the frames around the break, looped")
        self.crack_only.toggled.connect(lambda _on: self._set_range())
        self.dic_map = QtWidgets.QCheckBox("DIC map")
        self.dic_map.setToolTip("Overlay pyvale's vertical displacement where DIC ran on this frame")
        self.dic_map.toggled.connect(lambda _on: self._draw_frame())
        for b in (self.back10, self.back1, self.fwd1, self.fwd10):
            b.setFixedWidth(em(3.2))
        controls = flow(self.back10, self.back1, self.play_btn, self.fwd1, self.fwd10, self.speed, self.crack_btn,
                        self.crack_only, self.dic_map)

        self.info = QtWidgets.QLabel()
        self.info.setStyleSheet(status_style())
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.crack_text = QtWidgets.QLabel()
        self.crack_text.setWordWrap(True)

        self.curve_plot = pg.PlotWidget()
        self.curve_plot.showGrid(x=True, y=True, alpha=0.3)
        self.curve_plot.setMinimumHeight(em(7))
        self.curve_plot.setMaximumHeight(em(12))
        self.curve_plot.setMouseEnabled(x=True, y=False)
        self.curve = themed_pen(self.curve_plot.plot(), "load", 2)
        self.frame_dots = self.curve_plot.plot(pen=None, symbol="o", symbolSize=3, symbolPen=None,
                                               symbolBrush=pg.mkBrush(31, 95, 168, 120))
        self.break_marker = self.curve_plot.plot(pen=None, symbol="x", symbolSize=em(0.9),
                                                 symbolPen=pg.mkPen("#e67e22", width=3))
        self.now_line = themed_pen(pg.InfiniteLine(angle=90, movable=False), "accent", 2)
        self.curve_plot.addItem(self.now_line)
        self.regions: list = []
        self.curve_plot.scene().sigMouseClicked.connect(self._curve_clicked)
        tick_font = QtGui.QFont()
        tick_font.setPixelSize(max(11, em(0.8)))
        for side in ("left", "bottom"):
            self.curve_plot.getAxis(side).setStyle(tickFont=tick_font)
            self.curve_plot.getAxis(side).enableAutoSIPrefix(False)
        style_plot(self.curve_plot)

        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view, 3)
        layout.addWidget(self.slider)
        layout.addLayout(controls)
        layout.addWidget(self.info)
        layout.addWidget(self.crack_text)
        layout.addWidget(self.curve_plot, 1)
        return page

    def _build_plots(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        self.plot_list = QtWidgets.QListWidget()
        self.plot_list.setMaximumWidth(em(14))
        self.plot_list.currentRowChanged.connect(self._plot_selected)
        self.plot_list.itemDoubleClicked.connect(lambda _item: self._open_plot())
        self.plot_image = ScaledImage()
        self.plot_open = button("Open full size", self._open_plot)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(self.plot_image, 1)
        right.addWidget(self.plot_open)
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot_list)
        layout.addLayout(right, 1)
        return page

    # -------------------------------------------------------- availability --
    def set_available(self, on: bool) -> None:
        """Off while a test or a recording runs: stop playing and let go of the frames."""
        if on == self.available:
            return
        self.available = on
        if not on:
            self.stop_play()
            self._cache.clear()
            self._stale = True

    def on_shown(self) -> None:
        """The tab was opened: read the list (and the shown test's files) again if anything changed."""
        if not self.available:
            return
        if self._stale:
            self.refresh_list()
        elif self.res is not None:
            self.load(self.res.folder, keep_position=True)

    def mark_stale(self, newest: Path | None = None) -> None:
        """A test finished: read the list again when the tab opens, and show `newest` then."""
        self._stale = True
        if newest is not None:
            self._pending = str(newest)

    # --------------------------------------------------------------- listing --
    def refresh_list(self, then_show: str | None = None) -> None:
        if then_show is not None:
            self._pending = then_show
        if self._listing:
            return
        self._listing = True
        output_dir = self.win.config.output_dir

        def done(entries):
            self._listing = False
            self._stale = False
            self._fill(entries)

        def failed(exc):
            self._listing = False
            self.status.setText(f"Could not list the tests: {exc}")

        self.win.run_async(lambda: list_outputs(output_dir), done, failed, busy=False)

    def _fill(self, entries) -> None:
        current = self._pending or (str(self.res.folder) if self.res is not None else None)
        self._pending = None
        self.choice.blockSignals(True)
        self.choice.clear()
        for e in entries:
            try:
                started = datetime.fromisoformat(e.started).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                started = e.started
            what = e.stop_reason or e.status if e.kind == "test" else "camera only"
            self.choice.addItem(f"{started} · {e.name}" + (f" ({what})" if what else ""), str(e.path))
        self.choice.blockSignals(False)
        if not entries:
            self._show(None)
            self.status.setText(f"No tests in {self.win.config.output_dir} yet.")
            return
        index = self.choice.findData(current) if current else -1
        if current and index < 0:            # e.g. the camera/ folder of a listed test
            self.choice.insertItem(0, Path(current).name, current)
            index = 0
        self.choice.setCurrentIndex(max(index, 0))
        if current is None or self.res is None or str(self.res.folder) != self.choice.currentData():
            self._choose()

    def _choose(self) -> None:
        folder = self.choice.currentData()
        if folder:
            self.load(Path(folder))
            self._chosen(Path(folder))

    def _chosen(self, folder: Path) -> None:
        if self.on_chosen is not None:
            self.on_chosen(folder)

    def show_folder(self, folder: Path) -> None:
        """Show this test (the Test tab's and the Outputs tab's "View results")."""
        folder = str(Path(folder))
        if self._stale or self.choice.findData(folder) < 0:
            self.refresh_list(then_show=folder)
        else:
            self.choice.setCurrentIndex(self.choice.findData(folder))
            self.load(Path(folder))
            self._chosen(Path(folder))

    def current_folder(self) -> Path | None:
        folder = self.choice.currentData()
        return Path(folder) if folder else None

    def reload(self) -> None:
        self._stale = True
        self.refresh_list()
        if self.res is not None:
            self.load(self.res.folder, keep_position=True)

    # --------------------------------------------------------------- loading --
    def load(self, folder: Path, keep_position: bool = False) -> None:
        folder = Path(folder)
        self._generation += 1
        gen = self._generation
        keep = int(self.res.frames.index[self.pos]) if keep_position and self.res is not None \
            and self.res.frames is not None and len(self.res.frames) else None
        self._loading = str(folder)
        self.status.setText(f"Reading {folder.name}...")

        def done(res):
            if gen != self._generation:
                return
            self._loading = None
            self._show(res, keep)

        def failed(exc):
            if gen != self._generation:
                return
            self._loading = None
            self._show(None)
            self.status.setText(f"Cannot show {folder.name}: {exc}")

        self.win.run_async(lambda: load_results(folder), done, failed, busy=False)

    def _show(self, res: RunResults | None, keep_frame: int | None = None) -> None:
        self.stop_play()
        same = res is not None and self.res is not None and res.folder == self.res.folder
        if not same:
            self._cache.clear()
            self._shown_size = None
        self.res = res
        self.folder_btn.setEnabled(res is not None)
        self.video_btn.setEnabled(res is not None and res.video is not None)
        self._fill_summary()
        self._fill_plots()
        self._draw_curve()
        frames = res.frames if res is not None else None
        has = res is not None and res.has_images
        for w in (self.slider, self.back10, self.back1, self.play_btn, self.fwd1, self.fwd10, self.speed):
            w.setEnabled(has)
        crack = res.crack if res is not None else None
        self.crack_btn.setEnabled(has and crack is not None)
        self.crack_only.setEnabled(has and crack is not None)
        if crack is None:
            self.crack_only.setChecked(False)
        self.dic_map.setEnabled(has and (res.run / "pyvale_results").is_dir())
        self.status.setText(self._status_text())
        self.crack_text.setText(self._crack_text())
        self.crack_text.setVisible(bool(self.crack_text.text()))
        if not has:
            self.image.clear()
            self.dic_item.clear()
            self.roi_dots.setData([], [])
            self.notch_item.setData([], [])
            for item in self.tip_items.values():
                item.setData([], [])
            self.info.setText("")
            self._placeholder("No test selected" if res is None else
                              "No camera in this test" if frames is None else
                              "The camera images were deleted" if res.images_deleted else "No saved frames")
            if not same:
                self.inner.setCurrentIndex(1 if res is not None and res.plots else 0)
            return
        self._placeholder("")
        self._draw_roi()
        if keep_frame is not None:
            self.pos = frames.position_of(keep_frame)
        elif crack is not None:
            self.pos = crack.first if crack.n_frames else max(crack.first - 1, 0)
        else:
            self.pos = 0
        self._set_range()
        if crack is not None and crack.estimate is None and crack.full_rate and crack.n_frames >= 1:
            self._estimate(res)

    def _estimate(self, res: RunResults) -> None:
        gen = self._generation

        def done(pos):
            if gen != self._generation or self.res is not res:
                return
            self.crack_text.setText(self._crack_text())
            if pos is not None and self.res.crack.contains(self.pos) and self.pos == self.res.crack.first:
                self.go(pos)               # still on the interval's first frame: move to the estimate
            self._draw_frame()

        self.win.run_async(lambda: estimate_crack_frame(res), done, lambda exc: log.warning("crack estimate: %s",
                                                                                            exc), busy=False)

    # ---------------------------------------------------------------- texts --
    def _status_text(self) -> str:
        res = self.res
        if res is None:
            return ""
        parts = [str(res.folder)]
        job = self.win.postprocess.current
        busy = job is not None and res.run is not None and Path(job.run) == res.run
        queued = res.run is not None and any(Path(j.run) == res.run for j in self.win.postprocess.queue)
        if busy:
            parts.append(f"post-processing still running: {self.win.postprocess.text} (⟳ for the new results)")
        elif queued:
            parts.append("post-processing queued for this test (⟳ when it is done)")
        if res.frames is not None:
            parts.append(f"{len(res.frames)} frames saved")
        return " · ".join(parts)

    def _crack_text(self) -> str:
        res = self.res
        crack = res.crack if res is not None else None
        if crack is None:
            if res is not None and res.frames is not None and res.has_images:
                return "No break detected and no full-rate frames: no crack frames to show."
            return ""
        text = ""
        if crack.break_load_n is not None:
            text = f"Break load {self.units.format_force(crack.break_load_n)}. "
        text += crack.note
        if crack.estimate is not None:
            text += f" Estimated crack frame: {int(res.frames.index[crack.estimate])} (largest image change)."
        if crack.end >= crack.start:
            text += f" Crack window: {crack.end - crack.start + 1} frames."
        return text

    def _fill_summary(self) -> None:
        res = self.res
        if res is None:
            self.summary.setPlainText("")
            return
        lines = [res.summary_text.strip()] if res.summary_text else []
        pairing = res.sync.get("data_csv_pairing") or {}
        if res.frames is not None:
            s = res.settings
            lines.append("\nCamera")
            lines.append(f"  Frames grabbed / saved: {s.get('frames_grabbed', '?')} / {len(res.frames)}"
                         + (" (images deleted)" if res.images_deleted else ""))
            if s.get("fps_measured"):
                lines.append(f"  Frame rate: {s['fps_measured']:.0f} fps")
            if pairing:
                lines.append(f"  data.csv rows paired with a frame within {pairing['frame_dt_ms_p95']:.1f} ms (95 %),"
                             f" {pairing['frame_dt_ms_max']:.1f} ms at most")
            for key, label in (("dropped_frames", "Dropped by the camera"), ("ram_dropped_frames", "Dropped (RAM)"),
                               ("write_errors", "Write errors")):
                if s.get(key):
                    lines.append(f"  {label}: {s[key]}")
            windows = res.windows
            if windows:
                labels = s.get("window_labels") or []
                lines.append("  Full-rate windows: " + ", ".join(
                    f"frames {a}–{b}" + (f" ({labels[i]})" if i < len(labels) else "") for i, (a, b) in enumerate(windows)))
            if res.tips:
                lines.append("  Crack tips: " + ", ".join(f"{k} ({len(v)} frames)" for k, v in res.tips.items()))
        if res.crack is not None:
            lines.append("\nCrack\n  " + self._crack_text())
        warnings = res.meta.get("warnings") or []
        if warnings:
            lines.append("\nWarnings\n" + "\n".join(f"  {w}" for w in warnings))
        self.summary.setPlainText("\n".join(lines).strip() or "No summary for this recording.")

    def _fill_plots(self) -> None:
        self.plot_list.blockSignals(True)
        current = self.plot_list.currentItem().text() if self.plot_list.currentItem() else None
        self.plot_list.clear()
        for path in self.res.plots if self.res is not None else []:
            item = QtWidgets.QListWidgetItem(path.stem)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.plot_list.addItem(item)
        self.plot_list.blockSignals(False)
        row = 0
        if current:
            found = self.plot_list.findItems(current, Qt.MatchFlag.MatchExactly)
            row = self.plot_list.row(found[0]) if found else 0
        if self.plot_list.count():
            self.plot_list.setCurrentRow(row)
            self._plot_selected(row)
        else:
            self.plot_image.set_file(None)
        self.plot_open.setEnabled(self.plot_list.count() > 0)

    def _plot_selected(self, row: int) -> None:
        item = self.plot_list.item(row)
        self.plot_image.set_file(Path(item.data(Qt.ItemDataRole.UserRole)) if item else None)

    def _open_plot(self) -> None:
        item = self.plot_list.currentItem()
        if item is not None:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole)))

    # ---------------------------------------------------------------- curve --
    def _draw_curve(self) -> None:
        for region in self.regions:
            self.curve_plot.removeItem(region)
        self.regions = []
        u = self.units
        res = self.res
        self.curve_plot.setLabel("left", f"Load ({u.force})")
        self.curve_plot.setLabel("bottom", "Time (s)")
        t = res.data.get("time_s") if res is not None else None
        if t is not None and t.size:
            self.curve.setData(t, u.force_from_n(res.data["load_N"]), connect="finite")
        else:
            self.curve.setData([], [])
        frames = res.frames if res is not None else None
        if frames is not None and len(frames) and np.isfinite(frames.time_s).any():
            if t is None or not t.size:               # a recording without a test: the frames' loads
                self.curve.setData(frames.time_s, u.force_from_n(frames.load_n), connect="finite")
            self.frame_dots.setData(frames.time_s, u.force_from_n(np.nan_to_num(frames.load_n, nan=0.0)))
            for a, b in res.windows:
                sel = (frames.index >= a) & (frames.index <= b) & np.isfinite(frames.time_s)
                if sel.any():
                    self._region(frames.time_s[sel].min(), frames.time_s[sel].max(), FULL_RATE_BRUSH)
        else:
            self.frame_dots.setData([], [])
        crack = res.crack if res is not None else None
        if crack is not None and crack.t_hold is not None:
            delay = res.load_delay_s
            self._region(crack.t_hold - delay, crack.t_drop - delay, CRACK_BRUSH)
            self.break_marker.setData([crack.t_hold], [u.force_from_n(crack.break_load_n)])
        else:
            self.break_marker.setData([], [])
        self.now_line.setVisible(frames is not None and len(frames) > 0)
        self.curve_plot.enableAutoRange()

    def _region(self, a: float, b: float, brush) -> None:
        region = pg.LinearRegionItem(values=(a, b), movable=False, brush=pg.mkBrush(*brush),
                                     pen=pg.mkPen(None))
        region.setZValue(-10)
        self.curve_plot.addItem(region)
        self.regions.append(region)

    def _curve_clicked(self, event) -> None:
        res = self.res
        if res is None or not res.has_images:
            return
        vb = self.curve_plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        t = vb.mapSceneToView(event.scenePos()).x()
        self.go(res.frames.position_at(t))

    # -------------------------------------------------------------- frames --
    def _range(self) -> tuple[int, int]:
        res = self.res
        crack = res.crack if res is not None else None
        if self.crack_only.isChecked() and crack is not None and crack.end >= crack.start:
            return crack.start, crack.end
        return 0, len(res.frames) - 1

    def _set_range(self) -> None:
        if self.res is None or not self.res.has_images:
            return
        lo, hi = self._range()
        self.pos = min(max(self.pos, lo), hi)
        self.slider.blockSignals(True)
        self.slider.setRange(lo, hi)
        self.slider.setPageStep(max(1, (hi - lo) // 20))
        self.slider.setValue(self.pos)
        self.slider.blockSignals(False)
        self._draw_frame()

    def _slider_moved(self, value: int) -> None:
        self.pos = value
        self._draw_frame()

    def go(self, position: int) -> None:
        if self.res is None or not self.res.has_images:
            return
        lo, hi = self._range()
        if not lo <= position <= hi:
            self.crack_only.setChecked(False)          # jumping outside the crack window
            lo, hi = self._range()
        self.pos = min(max(int(position), lo), hi)
        self.slider.setValue(self.pos)                 # draws (valueChanged), or nothing if unchanged
        self._draw_frame()

    def step(self, n: int) -> None:
        if self.res is not None and self.res.has_images:
            lo, hi = self._range()
            self.go(min(max(self.pos + n, lo), hi))

    def jump_to_crack(self) -> None:
        crack = self.res.crack if self.res is not None else None
        if crack is None:
            return
        target = crack.estimate if crack.estimate is not None else (crack.first if crack.n_frames else
                                                                     max(crack.first - 1, 0))
        self.go(target)

    def _frame(self, position: int) -> np.ndarray | None:
        key = (str(self.res.folder), position)
        image = self._cache.get(key)
        if image is not None:
            self._cache.move_to_end(key)
            return image
        try:
            image = tiff.read(self.res.image_path(position))
        except (OSError, ValueError, ImportError) as e:
            log.info("cannot read frame %s: %s", position, e)
            return None
        self._cache[key] = image
        while len(self._cache) > CACHE_FRAMES:
            self._cache.popitem(last=False)
        return image

    def _draw_frame(self) -> None:
        res = self.res
        if res is None or not res.has_images or not self.available:
            return
        pos = self.pos
        frames = res.frames
        image = self._frame(pos)
        if image is None:
            self.image.clear()
            self._placeholder(f"Cannot read {frames.files[pos]}")
        else:
            self._placeholder("")
            self.image.setImage(image, autoLevels=False, levels=(0, 255))
            size = image.shape
            if size != self._shown_size:
                self._shown_size = size
                self.image.setRect(QtCore.QRectF(0, 0, size[1], size[0]))
                self.vb.setRange(QtCore.QRectF(0, 0, size[1], size[0]), padding=0.01)
        overlay = dic_overlay(res.dic_path(pos)) if self.dic_map.isChecked() else None
        if overlay is not None:
            self.dic_item.setImage(overlay[0], autoLevels=False)
            self.dic_item.setRect(overlay[1])
            self.dic_item.setVisible(True)
        else:
            self.dic_item.setVisible(False)
        tips = res.tips_at(pos)
        for name, item in self.tip_items.items():
            if name in tips:
                item.setData([tips[name][0] + 0.5], [tips[name][1] + 0.5])
            else:
                item.setData([], [])
        t = frames.time_s[pos]
        if math.isfinite(t):
            self.now_line.setValue(t)
        self.info.setText(self._frame_text(pos, tips))

    def _frame_text(self, pos: int, tips: dict) -> str:
        res, u = self.res, self.units
        frames, crack = res.frames, res.crack
        t = frames.time_s[pos]
        parts = [f"frame {int(frames.index[pos])} ({pos + 1}/{len(frames)})",
                 f"t {t:.4f} s" if math.isfinite(t) else "t —",
                 f"load {u.format_force(frames.load_n[pos])}", f"ext {u.format_length(frames.extension_mm[pos])}"]
        if frames.full_rate[pos]:
            parts.append("full rate")
        if frames.markers[pos]:
            parts.append(f"marker {frames.markers[pos]}")
        if tips:
            parts.append("tip: " + ", ".join(tips))
        text = "  ·  ".join(parts)
        if crack is not None and crack.estimate == pos:
            text += f"  <b style='color:{theme().danger}'>★ CRACK (estimated frame)</b>"
        elif crack is not None and crack.source == "break" and crack.contains(pos):
            text += f"  <b style='color:{theme().danger}'>CRACK</b>"
        return text

    def _draw_roi(self) -> None:
        roi = self.res.roi if self.res is not None else None
        if roi is not None and roi.mask.any():
            xs, ys = outline(roi.mask, step=2)
            self.roi_dots.setData(xs + 0.5, ys + 0.5)
        else:
            self.roi_dots.setData([], [])
        if roi is not None and roi.notch:
            self.notch_item.setData([roi.notch[0] + 0.5], [roi.notch[1] + 0.5])
        else:
            self.notch_item.setData([], [])

    def _placeholder(self, text: str) -> None:
        self.placeholder.setText(text)
        self.placeholder.setVisible(bool(text))
        if text:
            self._shown_size = None
            self.placeholder.setPos(320, 80)
            self.vb.setRange(QtCore.QRectF(0, 0, 640, 160), padding=0.02)

    # ------------------------------------------------------------- playing --
    def toggle_play(self) -> None:
        if self.play_btn.isChecked():
            if self.res is None or not self.res.has_images:
                self.play_btn.setChecked(False)
                return
            lo, hi = self._range()
            if self.pos >= hi and not self.crack_only.isChecked():
                self.go(lo)
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
        if self.res is None or not self.res.has_images or not self.available:
            self.stop_play()
            return
        lo, hi = self._range()
        if self.pos >= hi:
            if self.crack_only.isChecked():
                self.go(lo)                            # loop the crack window
            else:
                self.stop_play()
            return
        self.go(self.pos + 1)

    # ---------------------------------------------------------------- misc --
    def open_folder(self) -> None:
        if self.res is not None:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.res.folder)))

    def play_video(self) -> None:
        if self.res is not None and self.res.video is not None:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.res.video)))

    def apply_units(self, units: Units) -> None:
        self.units = units
        if self.res is not None:
            self._draw_curve()
            self._fill_summary()
            self.crack_text.setText(self._crack_text())
            self._draw_frame()

    def refresh_status(self) -> None:
        """Slow GUI tick: keep the post-processing note current."""
        if self.res is not None and self._loading is None:
            self.status.setText(self._status_text())


class ResultsTab(QtWidgets.QWidget):
    """The Results tab: one test picker over Overview (frames, plots, summary) and DIC analysis
    (run DIC, displacement, strain, crack tip, log) of the chosen test."""

    def __init__(self, win):
        super().__init__()
        from vsbc.gui.dic_view import DicView    # dic_view imports this module's neighbours

        self.win = win
        self.overview = ResultsView(win, own_bar=False)
        self.dic = DicView(win, linked=True)
        self.overview.on_chosen = self.dic.show_folder
        self.inner = FlowTabs()
        self.inner.addTab(self.overview, "Overview")
        self.inner.addTab(self.dic, "DIC analysis")
        self.inner.currentChanged.connect(lambda _i: self.on_shown())
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.overview.top_bar)
        layout.addWidget(self.inner, 1)

    def on_shown(self) -> None:
        """The tab (or one of its two pages) was opened: the picker always reads the list."""
        self.overview.on_shown()
        if self.inner.currentWidget() is self.dic:
            folder = self.overview.current_folder()
            if folder is not None:
                self.dic.show_folder(folder)
            self.dic.on_shown()

    def set_available(self, on: bool) -> None:
        self.overview.set_available(on)
        self.dic.set_available(on)

    def mark_stale(self, newest=None) -> None:
        self.overview.mark_stale(newest)
        self.dic.mark_stale(newest)

    def refresh_status(self) -> None:
        if self.inner.currentWidget() is self.overview:
            self.overview.refresh_status()
        self.dic.refresh_status()

    def apply_units(self, units) -> None:
        self.overview.apply_units(units)
        self.dic.apply_units(units)
