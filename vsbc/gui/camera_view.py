"""The Camera tab (left side, the default tab): live preview, the ROI editor,
markers, the high-speed button and manual recording.

Preview frames arrive downscaled (or as a 1:1 centre crop for focusing). The
image item is placed in full-resolution coordinates of the recorded frames,
so the ROI, the seed and the notch tip are drawn and edited in exactly the
pixels pyvale and the crack trackers use.
"""

from __future__ import annotations

import logging
import time

from vsbc.camera.roi import Roi, outline, polygon_mask, rect_mask
from vsbc.gui.common import FlowLayout, QtCore, QtWidgets, button, em, flow, high_speed_style, np, pg

log = logging.getLogger(__name__)

ROI_PEN = "#00c8ff"


def status_style() -> str:
    return (f"QLabel {{ font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: {max(11, em(0.85))}px; }}")


def countdown_style() -> str:
    return (f"QLabel {{ background: #7d3c98; color: white; font-size: {em(1.4)}px; font-weight: bold; "
            f"padding: {em(0.4)}px; border-radius: {em(0.3)}px; }}")


def dic_overlay(path):
    """(RGBA image, QRectF in image pixels) of the vertical displacement in one of pyvale's result CSVs
    (ss_x, ss_y, u, v, m, conv, ... by position), or None when it cannot be read."""
    try:
        data = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    except (OSError, ValueError):
        return None
    if data.shape[1] < 6:
        return None
    x, y, v, conv = data[:, 0], data[:, 1], data[:, 3], data[:, 5].astype(bool)
    xs, ys = np.unique(x), np.unique(y)
    if xs.size < 2 or ys.size < 2:
        return None
    grid = np.full((ys.size, xs.size), np.nan)
    grid[np.searchsorted(ys, y), np.searchsorted(xs, x)] = np.where(conv, v, np.nan)
    vmax = max(float(np.nanmax(np.abs(grid))) if np.isfinite(grid).any() else 0.0, 0.05)
    lut = pg.colormap.get("CET-D1").getLookupTable(nPts=256)
    idx = np.clip(((np.nan_to_num(grid) / vmax + 1) * 127.5).astype(int), 0, 255)
    rgba = np.zeros(grid.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = lut[idx][..., :3]
    rgba[..., 3] = np.where(np.isfinite(grid), 150, 0)
    dx, dy = xs[1] - xs[0], ys[1] - ys[0]
    return rgba, QtCore.QRectF(xs[0] - dx / 2, ys[0] - dy / 2, dx * xs.size, dy * ys.size)


class CameraView(QtWidgets.QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.editing = False
        self._seq = -1
        self._frame_size: tuple[int, int] | None = None
        self._zoomed = False
        self._countdown_end: float | None = None
        self._last_status = 0.0
        self._dic_text = ""
        self._shape_item = None
        self._shape = "rect"
        self._click_mode: str | None = None
        self._edit_seed = self._edit_notch = None
        self._edit_left = True
        self._waiting_snapshot = False
        self.snapshot: np.ndarray | None = None

        self.start_btn = button("Start camera", self.toggle_camera)
        self.exp_down = button("Exposure −", lambda: self._exposure(1 / 1.1))
        self.exp_up = button("Exposure +", lambda: self._exposure(1.1))
        self.zoom_btn = button("1:1 zoom", self.toggle_zoom)
        self.zoom_btn.setCheckable(True)
        self.zoom_btn.setToolTip("Full-resolution centre crop, for checking the focus")
        self.roi_btn = button("Edit ROI", self.edit_roi)
        self.marker_btn = button("Marker", self.marker)
        self.record_btn = button("● Record", self.toggle_record)
        self.record_btn.setToolTip("Record without a test (still-specimen noise checks, setup)")
        self.dic_map = QtWidgets.QCheckBox("DIC map")
        self.dic_map.setToolTip("Overlay the newest live-DIC vertical displacement (needs live DIC on)")
        self.dic_map.toggled.connect(lambda on: self.dic_item.setVisible(on))
        self.hs_btn = button("⚡  HIGH SPEED: save every frame", self.toggle_high_speed, high_speed_style(), em(3))
        self.hs_btn.setCheckable(True)
        self.hs_btn.setToolTip("Keeps the last seconds held in RAM and saves every frame from now until you turn "
                               "it off or the test ends (the camera and the test stop together). Press it when "
                               "the break is near.")

        # One bar that wraps onto as many rows as the width needs (HIGH SPEED on its own when it is narrow).
        bar = flow(self.start_btn, self.exp_down, self.exp_up, self.zoom_btn, self.roi_btn, self.marker_btn,
                   self.record_btn, self.dic_map, self.hs_btn)

        self.countdown = QtWidgets.QLabel()
        self.countdown.setStyleSheet(countdown_style())
        self.countdown.setWordWrap(True)
        self.countdown.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.countdown.hide()

        self.view = pg.GraphicsLayoutWidget()
        self.view.setBackground("k")
        self.view.setMinimumHeight(em(4))         # a short screen keeps the buttons; the preview shrinks
        self.vb = self.view.addViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self.image = pg.ImageItem(axisOrder="row-major")
        self.vb.addItem(self.image)
        self.dic_item = pg.ImageItem(axisOrder="row-major")
        self.dic_item.setZValue(5)
        self.dic_item.setVisible(False)
        self.vb.addItem(self.dic_item)
        self.roi_dots = pg.ScatterPlotItem(size=2, pen=None, brush=pg.mkBrush(0, 200, 255, 200))
        self.roi_dots.setZValue(10)
        self.seed_item = pg.ScatterPlotItem(symbol="+", size=20, pen=pg.mkPen("#2ecc71", width=3), brush=None)
        self.seed_item.setZValue(11)
        self.notch_item = pg.ScatterPlotItem(symbol="t1", size=18, pen=pg.mkPen("#ff4df0", width=2),
                                             brush=pg.mkBrush(255, 77, 240, 120))
        self.notch_item.setZValue(11)
        self.notch_text = pg.TextItem("", color="#ff4df0", anchor=(0, 1))
        self.notch_text.setZValue(11)
        for item in (self.roi_dots, self.seed_item, self.notch_item, self.notch_text):
            self.vb.addItem(item)
        self.vb.scene().sigMouseClicked.connect(self._clicked)
        self.placeholder = pg.TextItem("", color="#dddddd", anchor=(0.5, 0.5))
        self.vb.addItem(self.placeholder)

        self.edit_bar = QtWidgets.QWidget()
        edit = FlowLayout(self.edit_bar)
        self.rect_btn = button("Rectangle", lambda: self._set_shape("rect"))
        self.poly_btn = button("Polygon", lambda: self._set_shape("poly"))
        self.seed_btn = button("Place seed", lambda: self._arm("seed"))
        self.notch_btn = button("Place notch tip", lambda: self._arm("notch"))
        self.dir_btn = button("Crack grows →", self._toggle_direction)
        self.save_roi_btn = button("Save ROI", self.save_roi)
        self.cancel_btn = button("Cancel", self.stop_editing)
        for w in (self.rect_btn, self.poly_btn, self.seed_btn, self.notch_btn, self.dir_btn, self.save_roi_btn,
                  self.cancel_btn):
            edit.addWidget(w)
        self.edit_hint = QtWidgets.QLabel()
        self.edit_hint.setWordWrap(True)
        self.edit_bar.hide()
        self.edit_hint.hide()

        self.lines = [QtWidgets.QLabel() for _ in range(3)]
        for label in self.lines:
            label.setStyleSheet(status_style())
            label.setWordWrap(True)          # long recording lines wrap instead of widening the window
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(bar)
        layout.addWidget(self.countdown)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.edit_bar)
        layout.addWidget(self.edit_hint)
        for label in self.lines:
            layout.addWidget(label)
        self._show_placeholder("Camera off")
        self.draw_roi()

    @property
    def camera(self):
        return self.win.camera

    # ------------------------------------------------------------- buttons --
    def toggle_camera(self) -> None:
        if self.camera.state in ("off", "error"):
            self.camera.start()
            self._show_placeholder("Starting the camera...")
        elif self.camera.state == "recording":
            self.win.warn("Camera", "Stop the recording first.")
        else:
            self.win.run_async(self.camera.stop, busy=False)

    def _exposure(self, factor: float) -> None:
        self.camera.send("exposure", factor=factor)

    def toggle_zoom(self) -> None:
        self.camera.send("zoom", on=self.zoom_btn.isChecked())

    def marker(self) -> None:
        self.camera.send("marker")
        self.win.status("Marker added")

    def toggle_high_speed(self) -> None:
        self.win.set_high_speed(self.hs_btn.isChecked())

    def show_high_speed(self, on: bool) -> None:
        self.hs_btn.blockSignals(True)
        self.hs_btn.setChecked(on)
        self.hs_btn.blockSignals(False)

    def toggle_record(self) -> None:
        if self.camera.recording is not None:
            self.win.stop_camera_recording()
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Record", "Name for this recording:", text="recording")
        if ok:
            self.win.start_manual_recording(name.strip() or "recording")

    def show_countdown(self, seconds: float) -> None:
        self._countdown_end = time.monotonic() + seconds if seconds > 0 else None
        self.countdown.setVisible(self._countdown_end is not None)

    # ------------------------------------------------------------- preview --
    def _show_placeholder(self, text: str) -> None:
        self.placeholder.setText(text)
        self.placeholder.setVisible(bool(text))
        if text and self._frame_size is None:
            self.placeholder.setPos(320, 80)
            self.vb.setRange(QtCore.QRectF(0, 0, 640, 160), padding=0.02)

    def refresh(self) -> None:
        """GUI timer: draw the newest preview frame and the status lines."""
        cam = self.camera
        if self._countdown_end is not None:
            left = self._countdown_end - time.monotonic()
            if left <= 0 or cam.state == "recording":
                self.show_countdown(0)
            else:
                self.countdown.setText(f"Hands off: reference frame in {left:.1f} s")
        if cam.preview is not None and cam.preview_seq != self._seq and not self.editing:
            self._seq = cam.preview_seq
            header, frame = cam.preview
            scale = float(header.get("scale") or 1.0)
            self.image.setImage(frame, autoLevels=False, levels=(0, 255))
            self.image.setRect(QtCore.QRectF(header.get("x0", 0), header.get("y0", 0),
                                             frame.shape[1] / scale, frame.shape[0] / scale))
            full = tuple(header.get("full") or frame.shape)
            zoom = bool(header.get("zoom"))
            if full != self._frame_size or zoom != self._zoomed:
                self._frame_size, self._zoomed = full, zoom
                if zoom:
                    self.vb.setRange(QtCore.QRectF(header["x0"], header["y0"], frame.shape[1], frame.shape[0]),
                                     padding=0)
                else:
                    self.vb.setRange(QtCore.QRectF(0, 0, full[1], full[0]), padding=0.01)
                self.draw_roi()
            self._show_placeholder("")
        now = time.monotonic()
        if now - self._last_status >= 0.25:
            self._last_status = now
            self._update_status()
            self._update_buttons()

    def _update_status(self) -> None:
        cam = self.camera
        info, st = cam.info, cam.status
        if cam.state in ("off", "error", "starting"):
            text = {"off": "Camera off.", "starting": "Starting the camera process..."}.get(cam.state, "")
            if cam.error:
                text = f"Camera: {cam.error}"
            self._set_line(0, text)
            self._set_line(1, self._script_note())
            if cam.state != "starting" and not cam.preview:
                self._show_placeholder("Camera off" if cam.state == "off" else "No camera")
        else:
            header = cam.preview[0] if cam.preview else {}
            fps = st.get("fps") or info.get("fps")
            temp = st.get("temperature_c")
            parts = [f"{info.get('model', '?')} {info.get('width', '?')}×{info.get('height', '?')}",
                     f"{fps:.0f} fps" if fps else "fps ?", f"exp {st.get('exposure_us') or info.get('exposure_us', 0):.0f} µs",
                     f"bright99.9 {header.get('p999', 0):.0f}", f"sat {header.get('sat', 0):.2f} %"]
            if temp is not None:
                parts.append(f"{temp:.0f} °C")
            self._set_line(0, "  ·  ".join(parts))
            self._set_line(1, self._recording_text())
        pp = self.win.postprocess
        self._set_line(2, self._dic_text or (f"Post-processing: {pp.summary()}" if pp.busy or pp.done else ""))

    def _set_line(self, index: int, text: str) -> None:
        """Status line; an empty one takes no room (the preview gets it)."""
        self.lines[index].setText(text)
        self.lines[index].setVisible(bool(text))

    def _script_note(self) -> str:
        hello = self.camera.hello
        if hello.get("script_error"):
            return f"capture_dic_live.py did not load in the camera environment: {hello['script_error']}"
        return ""

    def _recording_text(self) -> str:
        cam, st = self.camera, self.camera.status
        if cam.state == "finishing":
            return f"Finishing the recording: {st.get('queue', '?')} frames left to write..."
        if cam.state == "recording" and "rec_s" not in st:
            return "● REC starting..."
        if cam.state != "recording":
            roi = self.win.roi
            if roi is None:
                return "No ROI yet: Edit ROI before the first test (DIC needs it)."
            problems = roi.problems()
            if self._frame_size and not roi.matches(*self._frame_size):
                problems.append("it was drawn for another image size")
            return ("ROI: " + "; ".join(problems)) if problems else "Ready. Recording starts with each test."
        mode = {"match": "1 per load sample", "every": "every Nth frame", "all": "every frame"}.get(
            self.win.camera_panel.save_mode.currentData(), "")
        if st.get("fallback"):
            mode = "every Nth frame (no load samples)"
        free = st.get("disk_free")
        text = (f"● REC {st['rec_s']:.1f} s · frame {st.get('frames', 0)} · saved {st.get('saved', 0)} ({mode}) · "
                f"queue {st.get('queue', 0)} · write {st.get('write_mb_s', 0):.0f} MB/s"
                + (f" · {free / 1e9:.1f} GB free" if free else ""))
        if st.get("high_speed"):
            hold = st.get("hold_s")
            text += " · HIGH SPEED" + (f" (≈{hold:.0f} s before frames are dropped)" if hold else " (disk keeps up)")
        if st.get("dropped"):
            text += f" · {st['dropped']} full-rate frames dropped (RAM)"
        if st.get("skipped"):
            text += f" · {st['skipped']} skipped"
        if st.get("low_disk"):
            text += " · DISK ALMOST FULL"
        return text

    def _update_buttons(self) -> None:
        cam = self.camera
        running = cam.state in ("preview", "recording", "finishing")
        self.start_btn.setText("Stop camera" if cam.state not in ("off", "error") else "Start camera")
        self.start_btn.setEnabled(cam.state != "starting" and cam.state != "recording")
        self.zoom_btn.setEnabled(running)
        for w in (self.exp_down, self.exp_up):   # fixed while recording: DIC compares every frame with frame 0
            w.setEnabled(cam.state == "preview")
        self.roi_btn.setEnabled(cam.state == "preview" and not self.editing)
        self.marker_btn.setEnabled(cam.state == "recording")
        self.hs_btn.setEnabled(cam.state == "recording")
        manual = cam.recording is not None and not cam.recording.get("test")
        self.record_btn.setText("■ Stop recording" if cam.recording is not None else "● Record")
        self.record_btn.setEnabled((cam.state == "preview" and self.win.runner is None) or manual
                                   or (cam.state == "recording" and self.win.runner is not None))

    # -------------------------------------------------------------- events --
    def on_event(self, msg: dict) -> None:
        ev = msg.get("ev")
        if ev == "snapshot" and self._waiting_snapshot:
            self._waiting_snapshot = False
            self._start_editing(msg["image"])
        elif ev == "high_speed":
            self.show_high_speed(bool(msg.get("on")))
        elif ev == "recording":
            self.show_countdown(0)
        elif ev == "finished":
            self.show_high_speed(False)
        elif ev == "dic":
            self._dic_event(msg.get("msg") or [])
        elif ev in ("lost", "error") and msg.get("fatal", True):
            self._show_placeholder("No camera" if ev == "error" else "Camera stopped")

    def _dic_event(self, msg: list) -> None:
        kind = msg[0] if msg else ""
        if kind == "frame":
            _, i, mean_u, max_u, valid, tpf = msg
            self._dic_text = (f"Live DIC frame {i}: mean |u| {mean_u:.2f} px, max {max_u:.2f}, "
                              f"valid {100 * valid:.0f} %, {tpf:.2f} s/frame")
            if self.dic_map.isChecked() and self.camera.recording:
                self._show_dic_map(self.camera.recording["folder"] / "pyvale_results"
                                   / f"dic_results_frame_{int(i):06d}.csv")
        elif kind == "fill":
            self._dic_text = f"Live DIC filling in: {msg[1]} frames left"
        elif kind == "done":
            self._dic_text = f"Live DIC done: {msg[1]} frames, {msg[2]:.2f} s/frame"
        elif kind == "error":
            self._dic_text = f"Live DIC error: {msg[1]}"

    def _show_dic_map(self, path) -> None:
        overlay = dic_overlay(path)
        if overlay is not None:
            self.dic_item.setImage(overlay[0], autoLevels=False)
            self.dic_item.setRect(overlay[1])

    # ------------------------------------------------------------------ ROI --
    def draw_roi(self) -> None:
        """Outline, seed and notch of the saved ROI (or the one being edited)."""
        roi = self.win.roi
        seed = self._edit_seed if self.editing else (roi.seed if roi else None)
        notch = self._edit_notch if self.editing else (roi.notch if roi else None)
        left = self._edit_left if self.editing else (roi.crack_from_left if roi else True)
        if roi is not None and not self.editing:
            xs, ys = outline(roi.mask, step=2)
            self.roi_dots.setData(xs + 0.5, ys + 0.5)
        else:
            self.roi_dots.setData([], [])
        if seed:
            self.seed_item.setData([seed[0] + 0.5], [seed[1] + 0.5])
        else:
            self.seed_item.setData([], [])
        if notch:
            self.notch_item.setData([notch[0] + 0.5], [notch[1] + 0.5])
            self.notch_text.setText("notch → crack" if left else "crack ← notch")
            self.notch_text.setPos(notch[0] + (8 if left else -110), notch[1] - 8)
        else:
            self.notch_item.setData([], [])
            self.notch_text.setText("")

    def edit_roi(self) -> None:
        self._waiting_snapshot = True
        if not self.camera.send("snapshot"):
            self._waiting_snapshot = False
            self.win.warn("Edit ROI", "The camera is not running.")

    def _start_editing(self, image: np.ndarray) -> None:
        self.editing = True
        self.snapshot = np.array(image, copy=True)
        h, w = image.shape
        self.image.setImage(self.snapshot, autoLevels=False, levels=(0, 255))
        self.image.setRect(QtCore.QRectF(0, 0, w, h))
        self.vb.setRange(QtCore.QRectF(0, 0, w, h), padding=0.01)
        roi = self.win.roi if self.win.roi is not None and self.win.roi.matches(h, w) else None
        self._edit_seed = roi.seed if roi else None
        self._edit_notch = roi.notch if roi else None
        self._edit_left = roi.crack_from_left if roi else True
        self.dir_btn.setText("Crack grows →" if self._edit_left else "Crack grows ←")
        if roi is not None and roi.mask.any():
            ys, xs = np.nonzero(roi.mask)
            box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
        else:
            box = (w * 0.05, h * 0.1, w * 0.95, h * 0.9)
        self._make_shape("rect", box)
        self.edit_bar.show()
        self.edit_hint.show()
        self._hint("Drag the box (corner handles resize it) to cover the speckle area, including the notch. "
                   "Then place the seed well inside it and the notch tip where the crack starts.")
        self.draw_roi()

    def _make_shape(self, shape: str, box) -> None:
        if self._shape_item is not None:
            self.vb.removeItem(self._shape_item)
        x0, y0, x1, y1 = box
        pen = pg.mkPen(ROI_PEN, width=2)
        if shape == "rect":
            item = pg.RectROI([x0, y0], [x1 - x0, y1 - y0], pen=pen)
            item.addScaleHandle([0, 0], [1, 1])
            item.addScaleHandle([1, 0], [0, 1])
            item.addScaleHandle([0, 1], [1, 0])
        else:
            item = pg.PolyLineROI([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], closed=True, pen=pen)
        item.setZValue(12)
        self.vb.addItem(item)
        self._shape_item, self._shape = item, shape

    def _shape_box(self):
        item = self._shape_item
        if self._shape == "rect":
            pos, size = item.pos(), item.size()
            return pos.x(), pos.y(), pos.x() + size.x(), pos.y() + size.y()
        pts = self._polygon_points()
        return pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()

    def _polygon_points(self) -> np.ndarray:
        item = self._shape_item
        pos = item.pos()
        return np.array([[p.x() + pos.x(), p.y() + pos.y()] for p in item.getState()["points"]])

    def _set_shape(self, shape: str) -> None:
        if self.editing and shape != self._shape:
            self._make_shape(shape, self._shape_box())
            if shape == "poly":
                self._hint("Drag the corners; click a line to add a corner, right-click a corner to remove it.")

    def _arm(self, mode: str) -> None:
        self._click_mode = mode
        self._hint("Click the image to place the " + ("seed (well inside the ROI)." if mode == "seed"
                                                      else "notch tip (where the crack starts)."))

    def _toggle_direction(self) -> None:
        self._edit_left = not self._edit_left
        self.dir_btn.setText("Crack grows →" if self._edit_left else "Crack grows ←")
        self.draw_roi()

    def _clicked(self, event) -> None:
        if not self.editing or self._click_mode is None:
            return
        pos = self.vb.mapSceneToView(event.scenePos())
        h, w = self.snapshot.shape
        x, y = int(np.clip(pos.x(), 0, w - 1)), int(np.clip(pos.y(), 0, h - 1))
        if self._click_mode == "seed":
            self._edit_seed = (x, y)
        else:
            self._edit_notch = (x, y)
        self._click_mode = None
        self._hint("")
        self.draw_roi()

    def _hint(self, text: str) -> None:
        self.edit_hint.setText(text)

    def _mask(self) -> np.ndarray:
        shape = self.snapshot.shape
        if self._shape == "rect":
            return rect_mask(shape, *self._shape_box())
        return polygon_mask(shape, self._polygon_points())

    def save_roi(self) -> None:
        mask = self._mask()
        seed = self._edit_seed
        if seed is None and mask.any():
            ys, xs = np.nonzero(mask)
            centre = np.array([xs.mean(), ys.mean()])
            k = int(np.argmin((xs - centre[0]) ** 2 + (ys - centre[1]) ** 2))
            seed = (int(xs[k]), int(ys[k]))
        info = self.camera.info
        roi = Roi(mask, seed, self._edit_notch, self._edit_left,
                  (int(info.get("offset_x") or 0), int(info.get("offset_y") or 0)))
        problems = roi.problems()
        if problems:
            self.win.warn("ROI", "Cannot save this ROI: " + "; ".join(problems) + ".")
            return
        if roi.notch is None and not self.win.ask(
                "ROI", "No notch tip placed: the crack-tip trackers will be skipped. Save anyway?"):
            return
        self.win.set_roi(roi, self.snapshot)
        self.stop_editing()
        self.win.status("ROI saved")

    def stop_editing(self) -> None:
        if self._shape_item is not None:
            self.vb.removeItem(self._shape_item)
            self._shape_item = None
        self.editing = False
        self._click_mode = None
        self.edit_bar.hide()
        self.edit_hint.hide()
        self._seq = -1
        self._frame_size = None
        self.draw_roi()
