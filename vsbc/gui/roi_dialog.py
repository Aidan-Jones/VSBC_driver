"""Draw a test's own ROI on one of its frames (DIC tab > Run > Draw ROI on a frame...).

For a test recorded without an ROI, or with the wrong one: the ROI belongs to
the test (camera/roi.npz), not to the Camera tab, since the specimen and the
crop can differ from trial to trial. Saving here never changes the Camera
tab's ROI (Outputs/camera/last_roi.npz).

DIC compares every frame with frame 0, so the dialog opens on frame 0; scrub
to check that the ROI still covers the specimen near the break.
"""

from __future__ import annotations

import math

from vsbc.camera import tiff
from vsbc.camera.roi import Roi, load_roi, polygon_mask, rect_mask, save_roi
from vsbc.gui.common import Qt, QtCore, QtWidgets, button, em, flow, np, pg, screen_size

ROI_PEN = "#00c8ff"


class RoiDialog(QtWidgets.QDialog):
    def __init__(self, win, data, on_saved=None):
        """`data` is the DIC tab's DicData for the test; on_saved(roi) runs after a save."""
        super().__init__(win)
        self.win = win
        self.data = data
        self.on_saved = on_saved
        self.run = data.res.run
        self.frames = sorted(data.files)
        self.saved_roi: Roi | None = None
        self.image: np.ndarray | None = None
        self.shape: tuple[int, int] | None = None
        self._item = None
        self._kind = "rect"
        self._click: str | None = None
        self.seed: tuple[int, int] | None = None
        self.notch: tuple[int, int] | None = None
        self.from_left = True
        self.setWindowTitle(f"ROI for {data.res.name or self.run.parent.name}")

        # ---- frame
        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(0, len(self.frames) - 1))
        self.slider.valueChanged.connect(self._show_frame)
        self.frame_label = QtWidgets.QLabel()
        self.ref_btn = button("Frame 0 (DIC reference)", lambda: self.slider.setValue(0))
        self.crack_btn = button("Near the break", self._to_break)
        self.crack_btn.setEnabled(data.res.crack is not None)

        # ---- image
        self.view = pg.GraphicsLayoutWidget()
        self.view.setBackground("k")
        self.view.setMinimumHeight(em(10))
        self.vb = self.view.addViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self.img = pg.ImageItem(axisOrder="row-major")
        self.vb.addItem(self.img)
        self.seed_item = pg.ScatterPlotItem(symbol="+", size=20, pen=pg.mkPen("#2ecc71", width=3), brush=None)
        self.notch_item = pg.ScatterPlotItem(symbol="t1", size=18, pen=pg.mkPen("#ff4df0", width=2),
                                             brush=pg.mkBrush(255, 77, 240, 120))
        self.notch_text = pg.TextItem("", color="#ff4df0", anchor=(0, 1))
        for item in (self.seed_item, self.notch_item, self.notch_text):
            item.setZValue(11)
            self.vb.addItem(item)
        self.vb.scene().sigMouseClicked.connect(self._clicked)

        # ---- editing
        self.rect_btn = button("Rectangle", lambda: self.set_kind("rect"))
        self.poly_btn = button("Polygon", lambda: self.set_kind("poly"))
        self.seed_btn = button("Place seed", lambda: self._arm("seed"))
        self.notch_btn = button("Place notch tip", lambda: self._arm("notch"))
        self.dir_btn = button("Crack grows →", self._toggle_direction)
        self.start = QtWidgets.QComboBox()
        self.start.addItem("Start from: this test's ROI", "test")
        self.start.addItem("Start from: the Camera tab's ROI", "camera")
        self.start.addItem("Start from: the whole frame", "frame")
        self.start.activated.connect(lambda _i: self._start_from(self.start.currentData()))
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.save_btn = button("Save for this test", self.save)
        self.cancel_btn = button("Cancel", self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(flow(QtWidgets.QLabel("Frame:"), self.ref_btn, self.crack_btn))
        layout.addWidget(self.slider)
        layout.addWidget(self.frame_label)
        layout.addWidget(self.view, 1)
        layout.addLayout(flow(self.rect_btn, self.poly_btn, self.seed_btn, self.notch_btn, self.dir_btn, self.start))
        layout.addWidget(self.hint)
        layout.addLayout(flow(self.save_btn, self.cancel_btn))

        self._show_frame(0)
        initial = "test" if self._test_roi() is not None else "camera" if self._camera_roi() is not None else "frame"
        self.start.setCurrentIndex(self.start.findData(initial))
        self._start_from(initial)
        self._hint("Drag the box (corner handles resize it) over the speckle area, including the notch, on frame 0: "
                   "DIC compares every frame with it. Then place the seed well inside the ROI and the notch tip "
                   "where the crack starts. Scrub to check it still covers the specimen near the break.")

    # ------------------------------------------------------------- opening --
    def open_dialog(self) -> None:
        width, height = screen_size(self.win)
        self.resize(min(em(70), width - em(2)), min(em(45), height - em(2)))
        run = getattr(self, "exec", None) or self.exec_
        run()

    # -------------------------------------------------------------- frames --
    def _show_frame(self, k: int) -> None:
        if not self.frames:
            self.frame_label.setText("This test has no saved frames.")
            self.save_btn.setEnabled(False)
            return
        frame = self.frames[min(max(k, 0), len(self.frames) - 1)]
        try:
            image = tiff.read(self.data.res.images_dir / self.data.files[frame])
        except (OSError, ValueError, ImportError) as e:
            self.frame_label.setText(f"Cannot read frame {frame}: {e}")
            return
        first = self.shape is None
        self.image, self.shape = image, image.shape
        self.img.setImage(image, autoLevels=False, levels=(0, 255))
        self.img.setRect(QtCore.QRectF(0, 0, image.shape[1], image.shape[0]))
        if first:
            self.vb.setRange(QtCore.QRectF(0, 0, image.shape[1], image.shape[0]), padding=0.01)
        t = self.data.time.get(frame, math.nan)
        text = f"Frame {frame}" + (f", t = {t:.3f} s" if math.isfinite(t) else "")
        if frame == 0:
            text += " (the DIC reference: draw the ROI on this one)"
        self.frame_label.setText(text)

    def _to_break(self) -> None:
        crack = self.data.res.crack
        if crack is not None and self.data.res.frames is not None:
            pos = min(crack.first, len(self.data.res.frames) - 1)
            frame = int(self.data.res.frames.index[pos])
            self.slider.setValue(int(np.searchsorted(self.frames, frame)))

    # ---------------------------------------------------------------- shape --
    def _test_roi(self) -> Roi | None:
        roi = self.data.res.roi
        return roi if roi is not None and self.shape is not None and roi.shape == self.shape else None

    def _camera_roi(self) -> Roi | None:
        roi = getattr(self.win, "roi", None)
        if roi is None:
            roi = load_roi(self.win.roi_file)
        return roi if roi is not None and self.shape is not None and roi.shape == self.shape else None

    def _start_from(self, which: str) -> None:
        if self.shape is None:
            return
        h, w = self.shape
        roi = self._test_roi() if which == "test" else self._camera_roi() if which == "camera" else None
        if which != "frame" and roi is None:
            self._hint("There is no such ROI for frames of this size: starting from the whole frame.")
        if roi is not None and roi.mask.any():
            ys, xs = np.nonzero(roi.mask)
            box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
            self.seed, self.notch, self.from_left = roi.seed, roi.notch, roi.crack_from_left
        else:
            box = (w * 0.05, h * 0.1, w * 0.95, h * 0.9)
            self.seed = self.notch = None
            self.from_left = True
        self._make("rect", box)
        self.dir_btn.setText("Crack grows →" if self.from_left else "Crack grows ←")
        self._draw_points()

    def _make(self, kind: str, box) -> None:
        if self._item is not None:
            self.vb.removeItem(self._item)
        x0, y0, x1, y1 = box
        pen = pg.mkPen(ROI_PEN, width=2)
        if kind == "rect":
            item = pg.RectROI([x0, y0], [x1 - x0, y1 - y0], pen=pen)
            item.addScaleHandle([0, 0], [1, 1])
            item.addScaleHandle([1, 0], [0, 1])
            item.addScaleHandle([0, 1], [1, 0])
        else:
            item = pg.PolyLineROI([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], closed=True, pen=pen)
        item.setZValue(12)
        self.vb.addItem(item)
        self._item, self._kind = item, kind

    def box(self) -> tuple[float, float, float, float]:
        if self._kind == "rect":
            pos, size = self._item.pos(), self._item.size()
            return pos.x(), pos.y(), pos.x() + size.x(), pos.y() + size.y()
        pts = self._points()
        return pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()

    def _points(self) -> np.ndarray:
        pos = self._item.pos()
        return np.array([[p.x() + pos.x(), p.y() + pos.y()] for p in self._item.getState()["points"]])

    def set_kind(self, kind: str) -> None:
        if kind != self._kind and self._item is not None:
            self._make(kind, self.box())
            if kind == "poly":
                self._hint("Drag the corners; click a line to add a corner, right-click a corner to remove it.")

    def set_box(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """A rectangle ROI (also used by the tests)."""
        self._make("rect", (x0, y0, x1, y1))

    def mask(self) -> np.ndarray:
        if self._kind == "rect":
            return rect_mask(self.shape, *self.box())
        return polygon_mask(self.shape, self._points())

    # --------------------------------------------------------------- points --
    def _arm(self, what: str) -> None:
        self._click = what
        self._hint("Click the frame to place the " + ("seed (well inside the ROI)." if what == "seed"
                                                      else "notch tip (where the crack starts)."))

    def _toggle_direction(self) -> None:
        self.from_left = not self.from_left
        self.dir_btn.setText("Crack grows →" if self.from_left else "Crack grows ←")
        self._draw_points()

    def _clicked(self, event) -> None:
        if self._click is None or self.shape is None:
            return
        pos = self.vb.mapSceneToView(event.scenePos())
        h, w = self.shape
        self.place(self._click, int(np.clip(pos.x(), 0, w - 1)), int(np.clip(pos.y(), 0, h - 1)))

    def place(self, what: str, x: int, y: int) -> None:
        if what == "seed":
            self.seed = (x, y)
        else:
            self.notch = (x, y)
        self._click = None
        self._hint("")
        self._draw_points()

    def _draw_points(self) -> None:
        if self.seed:
            self.seed_item.setData([self.seed[0] + 0.5], [self.seed[1] + 0.5])
        else:
            self.seed_item.setData([], [])
        if self.notch:
            self.notch_item.setData([self.notch[0] + 0.5], [self.notch[1] + 0.5])
            self.notch_text.setText("notch → crack" if self.from_left else "crack ← notch")
            self.notch_text.setPos(self.notch[0] + (8 if self.from_left else -110), self.notch[1] - 8)
        else:
            self.notch_item.setData([], [])
            self.notch_text.setText("")

    def _hint(self, text: str) -> None:
        self.hint.setText(text)

    # ----------------------------------------------------------------- save --
    def build(self) -> Roi:
        mask = self.mask()
        seed = self.seed
        if (seed is None or not mask[seed[1], seed[0]]) and mask.any():
            ys, xs = np.nonzero(mask)                 # the ROI pixel nearest its centre
            k = int(np.argmin((xs - xs.mean()) ** 2 + (ys - ys.mean()) ** 2))
            seed = (int(xs[k]), int(ys[k]))
        s = self.data.res.settings
        offsets = tuple(int(s[k]) if s.get(k) is not None else -1 for k in ("offset_x", "offset_y"))
        return Roi(mask, seed, self.notch, self.from_left, offsets)

    def save(self) -> bool:
        roi = self.build()
        problems = roi.problems()
        if problems:
            self.win.warn("ROI", "Cannot save this ROI: " + "; ".join(problems) + ".")
            return False
        if roi.notch is None and not self.win.ask(
                "ROI", "No notch tip placed: the crack-tip trackers will be skipped for this test. Save anyway?"):
            return False
        n_dic = len(self.data.dic)
        if (self.run / "roi.npz").is_file() and n_dic and not self.win.ask(
                "ROI", f"Replace this test's ROI? {n_dic} frames have DIC results made with the old one: run DIC "
                       "again with Redo ticked to recompute them."):
            return False
        try:
            save_roi(self.run / "roi.npz", roi)
            if self.image is not None:
                tiff.write_gray8(self.run / "roi_snapshot.tif", self.image)
        except OSError as e:
            self.win.warn("ROI", f"Could not save the ROI: {e}")
            return False
        self.saved_roi = roi
        if self.on_saved is not None:
            self.on_saved(roi)
        self.accept()
        return True
