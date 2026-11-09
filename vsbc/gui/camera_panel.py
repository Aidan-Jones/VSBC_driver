"""Camera tab on the right: recording, sync, DIC and post-processing settings.

Camera settings proper (exposure, crop, frame rate, buffer, DIC parameters)
are the USER SETTINGS in camera/capture_dic_live.py; the camera process sends
them when it starts and they become the defaults here. Changes made here
last for the session. config.toml [camera] sets the rest.
"""

from __future__ import annotations

import logging
from pathlib import Path

from vsbc.camera.postprocess import Options
from vsbc.camera.scripts import check_scripts, literal_settings
from vsbc.gui.common import QtCore, QtGui, QtWidgets, button, flow, form_layout, narrow_combo, spin
from vsbc.gui.panels import Panel
from vsbc.storage import disk_status, human_size

log = logging.getLogger(__name__)

SAVE_MODES = (
    ("One frame per load sample (1:1 with data.csv)", "match"),
    ("Every Nth camera frame", "every"),
    ("Every camera frame (full rate)", "all"),
)


def _ispin(lo: int, hi: int, value: int) -> QtWidgets.QSpinBox:
    box = QtWidgets.QSpinBox()
    box.setRange(lo, hi)
    box.setValue(value)
    box.setKeyboardTracking(False)
    return box


def _check(text: str, checked: bool, tip: str = "") -> QtWidgets.QCheckBox:
    box = QtWidgets.QCheckBox(text)
    box.setChecked(checked)
    if tip:
        box.setToolTip(tip)
    return box


class CameraPanel(Panel):
    def __init__(self, win):
        super().__init__(win)
        c = win.config.camera

        # --- camera (read-only) ---
        cam_box = QtWidgets.QGroupBox("Camera (set in camera/capture_dic_live.py)")
        self.camera_info = QtWidgets.QLabel("Camera off.")
        self.camera_info.setWordWrap(True)
        QtWidgets.QVBoxLayout(cam_box).addWidget(self.camera_info)

        # --- recording ---
        rec_box = QtWidgets.QGroupBox("Recording")
        self.record_with_test = _check("Record with each test", c.record_with_test,
                                       "The camera starts with each test and stops when it ends.")
        self.save_mode = narrow_combo(QtWidgets.QComboBox())
        for text, mode in SAVE_MODES:
            self.save_mode.addItem(text, mode)
        self.save_mode.setCurrentIndex(max(0, self.save_mode.findData(c.save_mode)))
        self.divisor = _ispin(1, 1000, c.match_divisor)
        self.divisor.setPrefix("every ")
        self.divisor.setSuffix(" sample(s)")
        self.divisor.setToolTip("1 = a frame for every load sample. 2 = every other sample, half the disk space.")
        self.every = _ispin(1, 10000, 9)
        self.every.setPrefix("every ")
        self.every.setSuffix(" frame(s)")
        self.buffer = spin(0.1, 30.0, 2.0, 1, 0.5, "s")
        self.buffer.setToolTip("Every frame of the last this-many seconds is held in RAM (BUFFER_SECONDS), so the "
                               "frames just before the break, or before you press High speed, are kept.")
        self.dump_on_break = _check("Save full-rate frames at the break", c.dump_on_break,
                                    "When the Mega detects the break, every frame of the last few seconds (below) "
                                    "is saved at the full camera rate, so the frames just before the break are kept.")
        self.break_s = spin(0.05, 30.0, c.break_seconds, 2, 0.1, "s")
        self.break_s.setToolTip("How many seconds before the break are saved at full rate. The RAM buffer keeps at "
                                "least this much.")
        self.dump_on_break.toggled.connect(self.break_s.setEnabled)
        self.break_s.setEnabled(self.dump_on_break.isChecked())
        self.settle = spin(0.0, 60.0, 5.0, 1, 1.0, "s")
        self.settle.setToolTip("Wait before the reference frame (hands off the machine)")
        self.estimate = QtWidgets.QLabel()
        self.estimate.setWordWrap(True)
        form = form_layout(rec_box)
        form.addRow(self.record_with_test)
        form.addRow("Save", self.save_mode)
        form.addRow("1:1 frames", self.divisor)
        form.addRow("Nth frame", self.every)
        form.addRow("RAM buffer", self.buffer)
        form.addRow(self.dump_on_break)
        form.addRow("Before the break", self.break_s)
        form.addRow("Settle before frame 0", self.settle)
        form.addRow(self.estimate)
        self.save_mode.currentIndexChanged.connect(self._mode_changed)

        # --- sync ---
        sync_box = QtWidgets.QGroupBox("Time sync")
        self.load_delay = spin(-1000.0, 1000.0, c.load_delay_ms, 1, 5.0, "ms")
        self.load_delay.setToolTip("How much later the load reading shows a change than the camera (the HX711's "
                                   "filter, roughly 2 sample periods). Measure it once with a tap test.")
        sync_note = QtWidgets.QLabel("Frames and data.csv share data.csv's clock (time_s). The load-cell delay "
                                     "shifts the load when pairing it with frames.")
        sync_note.setWordWrap(True)
        sform = form_layout(sync_box)
        sform.addRow("Load-cell delay", self.load_delay)
        sform.addRow(sync_note)

        # --- DIC ---
        dic_box = QtWidgets.QGroupBox("DIC")
        self.live_dic = _check("Live DIC during the test", c.live_dic,
                               "DIC while recording, shown in the Camera tab. On the Pi it competes with the camera "
                               "for the CPU and can cost frames: the DIC after the test is safer.")
        self.overview_every = _ispin(1, 10000, 3)
        self.overview_every.setPrefix("every ")
        self.overview_every.setSuffix(" saved frame(s)")
        self.crack_every = _ispin(1, 1000, 1)
        self.crack_every.setPrefix("every ")
        self.crack_every.setSuffix(" frame(s)")
        self.subset_size = _ispin(5, 401, 41)
        self.subset_size.setSingleStep(2)
        self.subset_step = _ispin(1, 200, 20)
        self.max_disp = _ispin(1, 1000, 64)
        self.dic_estimate = QtWidgets.QLabel()
        self.dic_estimate.setWordWrap(True)
        dform = form_layout(dic_box)
        dform.addRow(self.live_dic)
        dform.addRow("Whole test", self.overview_every)
        dform.addRow("Full-rate windows", self.crack_every)
        dform.addRow("Subset size (px)", self.subset_size)
        dform.addRow("Subset step (px)", self.subset_step)
        dform.addRow("Max displacement (px)", self.max_disp)
        dform.addRow(self.dic_estimate)

        # --- after the test ---
        post_box = QtWidgets.QGroupBox("After the test")
        self.make_video = _check("Video (video.mp4)", c.make_video,
                                 "Its playback time equals data.csv's time_s; each frame shows the time and the load.")
        self.video_fps = spin(1.0, 120.0, c.video_fps, 1, 5.0, "fps")
        self.run_dic = _check("DIC", c.run_dic, "capture_dic_live.py's dic_worker on the selected frames")
        self.run_strain = _check("Strain + plots", c.run_strain, "pyvale_dic_run.py; the plots go to camera/plots/")
        self.strain_animation = _check("Strain animation", c.strain_animation)
        self.crack_image = _check("Crack tip: image tracker", c.crack_tip_image, "crack_tip_image.py")
        self.crack_opening = _check("Crack tip: opening method", c.crack_tip_opening, "crack_tip_opening.py")
        self.process_last_btn = button("Process last test", self.process_last)
        self.process_last_btn.setToolTip("Queue the last recording's post-processing again")
        self.process_folder_btn = button("Process a folder...", self.process_folder)
        self.pause_btn = button("Pause", self.toggle_pause)
        self.cancel_btn = button("Cancel", self.cancel)
        self.post_status = QtWidgets.QLabel()
        self.post_status.setWordWrap(True)
        pform = form_layout(post_box)
        pform.addRow(self.make_video)
        pform.addRow("Video frame rate", self.video_fps)
        for box in (self.run_dic, self.run_strain, self.strain_animation, self.crack_image, self.crack_opening):
            pform.addRow(box)
        pform.addRow(flow(self.process_last_btn, self.process_folder_btn, self.pause_btn, self.cancel_btn))
        pform.addRow(self.post_status)

        # --- scripts ---
        scripts_box = QtWidgets.QGroupBox("Camera scripts")
        self.scripts_info = QtWidgets.QLabel()
        self.scripts_info.setWordWrap(True)
        self.scripts_info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reload_btn = button("Reload scripts", self.reload_scripts)
        self.reload_btn.setToolTip("Restarts the camera process so it loads the files in the camera folder again")
        self.open_scripts_btn = button("Open the folder", self.open_scripts)
        self.check_btn = button("Check camera, disk, pyvale...", self.run_check)
        slayout = QtWidgets.QVBoxLayout(scripts_box)
        slayout.addWidget(self.scripts_info)
        slayout.addLayout(flow(self.reload_btn, self.open_scripts_btn))
        slayout.addWidget(self.check_btn)

        layout = QtWidgets.QVBoxLayout(self)
        for box in (cam_box, rec_box, sync_box, dic_box, post_box, scripts_box):
            layout.addWidget(box)
        layout.addStretch(1)
        self.apply_script_settings(literal_settings(win.config.camera_dir))
        self._mode_changed()
        self.show_scripts()

    # -------------------------------------------------------------- settings --
    def apply_script_settings(self, s: dict) -> None:
        """capture_dic_live.py's USER SETTINGS become the defaults here."""
        save_every = max(1, int(s.get("SAVE_EVERY") or 9))
        self.every.setValue(save_every)
        self.buffer.setValue(float(s.get("BUFFER_SECONDS") or 2.0))
        self.settle.setValue(float(s.get("SETTLE_SECONDS") or 0.0))
        self.overview_every.setValue(max(1, int(s.get("DIC_STEP") or 27) // save_every))
        self.crack_every.setValue(max(1, int(s.get("CRACK_DIC_STEP") or 1)))
        self.subset_size.setValue(int(s.get("subset_size") or 41))
        self.subset_step.setValue(int(s.get("subset_step") or 20))
        self.max_disp.setValue(int(s.get("max_displacement") or 64))

    def on_event(self, msg: dict) -> None:
        ev = msg.get("ev")
        if ev == "hello":
            self.apply_script_settings(msg.get("settings") or {})
            self.show_scripts()
        if ev in ("hello", "opened", "lost", "error"):
            self.show_camera()

    def _mode_changed(self) -> None:
        mode = self.save_mode.currentData()
        self.divisor.setEnabled(mode == "match")
        self.every.setEnabled(mode == "every")

    def show_camera(self) -> None:
        cam = self.win.camera
        info, hello = cam.info, cam.hello
        if not info:
            text = cam.error or ("Starting..." if cam.state == "starting" else "Camera off.")
        else:
            fps = info.get("fps")
            text = (f"{info.get('model')} (serial {info.get('serial')}), {info.get('width')} × {info.get('height')} px "
                    f"at offset {info.get('offset_x')}, {info.get('offset_y')}; "
                    f"{'%.0f fps' % fps if fps else 'fps unknown'}; exposure {info.get('exposure_us', 0):.0f} µs; "
                    f"timestamp latch {'yes' if info.get('latch') else 'no (sync from arrival times)'}.")
        versions = hello.get("versions") or {}
        if versions:
            text += "\nCamera environment: " + ", ".join(f"{k} {v or 'missing'}" for k, v in versions.items())
        self.camera_info.setText(text)

    def show_scripts(self) -> None:
        folder = self.win.config.camera_dir
        lines = [f"Folder: {folder}"] + [c.text() for c in check_scripts(folder)]
        error = self.win.camera.hello.get("script_error") if self.win.camera else None
        if error:
            lines.append(f"✗ capture_dic_live.py does not load in the camera environment: {error}")
        self.scripts_info.setText("\n".join(lines))

    def recording_options(self, name: str = "") -> dict:
        live = None
        if self.live_dic.isChecked():
            live = {"overview_every": self.overview_every.value(), "crack_every": self.crack_every.value(),
                    "subset_size": self.subset_size.value(), "subset_step": self.subset_step.value(),
                    "max_displacement": self.max_disp.value()}
        return {"mode": self.save_mode.currentData(), "every": self.every.value(), "divisor": self.divisor.value(),
                "buffer_s": self.buffer.value(), "settle_s": self.settle.value(),
                "dump_on_break": self.dump_on_break.isChecked(), "break_s": self.break_s.value(),
                "load_delay_ms": self.load_delay.value(), "live_dic": live, "units": self.win.units.to_dict(),
                "name": name}

    def post_options(self) -> Options:
        return Options(run_dic=self.run_dic.isChecked(), run_strain=self.run_strain.isChecked(),
                       strain_animation=self.strain_animation.isChecked(), crack_tip_image=self.crack_image.isChecked(),
                       crack_tip_opening=self.crack_opening.isChecked(), make_video=self.make_video.isChecked(),
                       video_fps=self.video_fps.value(), overview_every=self.overview_every.value(),
                       crack_every=self.crack_every.value(), subset_size=self.subset_size.value(),
                       subset_step=self.subset_step.value(), max_displacement=self.max_disp.value())

    # -------------------------------------------------------------- estimates --
    def rates(self) -> dict:
        """Frame size, camera rate, saved rate and data rates for the current settings."""
        info = self.win.camera.info
        width, height = int(info.get("width") or 1920), int(info.get("height") or 400)
        fps = float(info.get("fps") or 100.0)
        expected = self.win.config.loadcell.rate_hz
        sps = float(self.win.machine.sample_rate_hz or expected) if self.win.machine.connected else expected
        frame = width * height
        mode = self.save_mode.currentData()
        saved_fps = {"match": sps / self.divisor.value(), "every": fps / self.every.value(), "all": fps}[mode]
        return {"frame_bytes": frame, "fps": fps, "sps": sps, "saved_fps": saved_fps,
                "ring_bytes": max(self.buffer.value(), self.break_s.value()) * fps * frame,
                "break_bytes": self.break_s.value() * fps * frame, "full_rate_mb_s": fps * frame / 1e6}

    def test_bytes(self, duration_s: float) -> float:
        r = self.rates()
        dump = r["break_bytes"] if self.dump_on_break.isChecked() else 0.0
        return r["saved_fps"] * duration_s * r["frame_bytes"] + dump

    def _test_duration_s(self) -> float:
        panel = self.win.test_panel
        rate = panel.rate.si_value()
        return 60.0 * panel.max_ext.si_value() / rate if rate > 0 else 0.0

    def update_estimate(self) -> None:
        r = self.rates()
        duration = self._test_duration_s()
        u = self.win.units
        try:
            free = disk_status(self.win.config.output_dir).free
        except OSError:
            free = None
        size = self.test_bytes(duration)
        test = self.win.test_panel
        text = (f"Saves ≈ {r['saved_fps']:.1f} frames/s ({r['saved_fps'] * r['frame_bytes'] / 1e6:.0f} MB/s); "
                f"load cell {r['sps']:.0f} samples/s, camera {r['fps']:.0f} fps.\n"
                f"Up to {duration / 60:.1f} min for this test (max extension "
                f"{u.format_length(test.max_ext.si_value(), 2)} at "
                f"{u.length_from_mm(test.rate.si_value()):g} {u.rate_min_label}): ≈ {human_size(size)}"
                + (f" of {human_size(free)} free." if free is not None else ".")
                + f"\nHigh speed adds {r['full_rate_mb_s']:.0f} MB per second while on. "
                  f"RAM buffer: {human_size(r['ring_bytes'])}.")
        self.estimate.setText(text)
        frames = r["saved_fps"] * duration / max(1, self.overview_every.value())
        self.dic_estimate.setText(f"Whole-test DIC: ≈ {frames:.0f} frames, plus the full-rate window(s).")

    def preflight(self) -> tuple[list[str], list[str], bool]:
        """(errors, warnings, record) for the next test. Without a running camera the test runs unrecorded."""
        errors, warnings = [], []
        cam = self.win.camera
        if cam.recording is not None or cam.state in ("recording", "finishing"):
            errors.append("A camera recording is still running or being written (Camera tab). Stop it, or wait "
                          "until it has finished, before starting the test.")
            return errors, warnings, False
        if cam.state != "preview":
            warnings.append(f"The camera is not running ({cam.error or cam.state}): this test is NOT recorded. "
                            "Start the camera in the Camera tab to record it.")
            return errors, warnings, False
        roi = self.win.roi
        info = cam.info
        if roi is None or roi.problems() or not roi.matches(info.get("height", 0), info.get("width", 0)):
            warnings.append("No usable ROI for this camera image (Camera tab > Edit ROI): the frames are recorded, "
                            "but DIC and the crack trackers are skipped.")
        elif roi.notch is None:
            warnings.append("No notch tip set (Camera tab > Edit ROI): the crack trackers are skipped.")
        try:
            status = disk_status(self.win.config.output_dir, self.win.config.storage.warn_free_gb,
                                 self.win.config.storage.critical_free_gb)
        except OSError:
            status = None
        need = self.test_bytes(self._test_duration_s())
        if status is not None:
            if need > status.free - 1024 ** 3:
                errors.append(f"Not enough disk space for the camera: this test can take up to {human_size(need)}, "
                              f"{human_size(status.free)} is free. Free space (Outputs tab), save fewer frames "
                              "(Camera panel), or lower the max extension.")
            elif status.level != "ok":
                warnings.append(f"Storage is low: {human_size(status.free)} free.")
        r = self.rates()
        ram = cam.status.get("ram_available") or cam.hello.get("ram_available")
        if ram and r["ring_bytes"] > 0.5 * ram:
            warnings.append(f"The RAM buffer ({human_size(r['ring_bytes'])}) is more than half the free RAM "
                            f"({human_size(ram)}): lower it in the Camera panel.")
        if self.live_dic.isChecked():
            warnings.append("Live DIC is on: on the Pi it competes with the camera and can cost frames.")
        return errors, warnings, True

    # ----------------------------------------------------------- processing --
    def process_last(self) -> None:
        folder = self.win.last_camera_folder
        if folder is None:
            self.win.inform("Process", "No recording in this session yet. Use Process a folder.")
            return
        self.win.queue_postprocess(folder)

    def process_folder(self) -> None:
        start = str(self.win.config.tests_dir if self.win.config.tests_dir.is_dir() else self.win.config.output_dir)
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Test or recording folder", start)
        if path:
            self.win.queue_postprocess(Path(path))

    def toggle_pause(self) -> None:
        pp = self.win.postprocess
        if pp.paused:
            pp.resume()
        else:
            pp.pause()

    def cancel(self) -> None:
        if self.win.ask("Cancel", "Cancel the post-processing that is running and queued? "
                                  "It can be run again later with Process a folder."):
            self.win.postprocess.cancel()

    def reload_scripts(self) -> None:
        cam = self.win.camera
        if cam.recording is not None:
            self.win.warn("Reload scripts", "Not while recording.")
            return
        self.show_scripts()
        self.win.run_async(cam.restart, lambda _r: self.show_scripts(), busy=False)

    def open_scripts(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.win.config.camera_dir)))

    def run_check(self) -> None:
        self.win.run_camera_check()

    # --------------------------------------------------------------- enabling --
    def update_enabled(self, connected: bool, busy: bool) -> None:
        cam = self.win.camera
        recording = cam.recording is not None or cam.state in ("recording", "finishing")
        for w in (self.save_mode, self.divisor, self.every, self.buffer, self.settle, self.live_dic,
                  self.dump_on_break, self.load_delay, self.reload_btn, self.check_btn):
            w.setEnabled(not recording)
        self.break_s.setEnabled(not recording and self.dump_on_break.isChecked())
        if not recording:
            self._mode_changed()
        pp = self.win.postprocess
        self.pause_btn.setText("Resume" if pp.paused else "Pause")
        self.pause_btn.setEnabled(pp.busy)
        self.cancel_btn.setEnabled(pp.busy)
        self.post_status.setText(pp.summary())
