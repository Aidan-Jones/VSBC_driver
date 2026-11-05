"""Main window: connection bar, STOP, live readouts, plots and the tabs.

Threading: the serial reader thread and worker threads never touch widgets.
They put items into `self.inbox`, and a QTimer on the Qt thread drains it
(about 25 times a second), updating the plots and readouts and running callbacks.
The camera process's events arrive the same way (CameraClient -> post).
"""

from __future__ import annotations

import logging
import math
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from vsbc.analysis import tension_threshold_n
from vsbc.config import REPO_ROOT, Config
from vsbc.gui.calibration_panel import CalibratePanel
from vsbc.gui.common import (
    FlowTabs, QShortcut, Qt, QtCore, QtGui, QtWidgets, apply_style, banner_style, button, caption_style, em,
    exec_app, narrow_combo, readout_style, screen_size, stop_style,
)
from vsbc.gui.outputs_panel import OutputsPanel
from vsbc.gui.panels import ConsolePanel, FirmwarePanel, JogPanel, TestPanel
from vsbc.gui.plots import LivePlots
from vsbc.gui.units_panel import UnitsPanel
from vsbc.link import DeviceError, LinkClosed, list_ports
from vsbc.machine import FirmwareMismatch, Machine
from vsbc.procedures import PreflightError, PullTestRunner
from vsbc.protocol import STATE_NAMES, Event, Reply, Sample
from vsbc.storage import disk_status, human_size
from vsbc.units import Units, clear_saved_units, effective_units, save_units

log = logging.getLogger(__name__)

STOP_REASONS = {
    "BREAK": "specimen break detected",
    "LOAD": "stop load reached",
    "OVERLOAD": "overload cutoff reached",
    "LOADCELL": "the load cell stopped responding",
    "WATCHDOG": "the app stopped talking to the Mega (watchdog)",
    "TIMEOUT": "the move took too long",
    "LIMIT": "limit switch",
    "ESTOP": "E-stop input",
}
SAFETY_STOPS = {"OVERLOAD", "LOADCELL", "WATCHDOG", "TIMEOUT", "LIMIT", "ESTOP"}
STORAGE_CHECK_MS = 30_000
TENSION_WARNING = ("Tension is reading negative: the load-cell sign is reversed (Calibrate tab > Flip load sign). "
                   "Break detection cannot arm until it is fixed.")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config: Config, *, port: str | None = None, sim=False,
                 auto_connect: bool = False, interactive: bool = True):
        super().__init__()
        self.config = config
        self.interactive = interactive          # False in tests: dialogs answer "yes" by themselves
        self.units: Units = effective_units(config)
        self.machine = Machine(config)
        self.machine.add_listener(self._on_message)
        self.machine.add_raw_listener(self._on_raw)
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.busy_ops = 0
        self.runner: PullTestRunner | None = None
        self.last_result = None
        self.test_start_pos: float | None = None
        self.last_ext: float | None = None
        self._connecting = False
        self._ready_seen = False
        self._saved_position = False
        self._warnings: list[str] = []
        self._tension = {"count": 0, "max": -math.inf, "prev": -math.inf, "warned": False}

        # Camera (the Camera tab), post-processing and the region of interest
        self.camera = None
        self.roi = None
        self.roi_snapshot = None
        self.last_camera_folder: Path | None = None
        self._pending_sync: Path | None = None
        self._manual_folder: Path | None = None
        from vsbc.camera.postprocess import PostProcessor

        self.postprocess = PostProcessor(config)
        if config.camera.enabled:
            from vsbc.camera.client import CameraClient

            self.camera = CameraClient(config, self.machine, on_event=lambda msg: self.post(self._on_camera_event, msg),
                                       sim=bool(sim))
            self._load_roi()

        self.setWindowTitle("VSBC Tensile Tester")
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(max(10, int(1000 / config.gui.refresh_hz)))
        self._last_slow_tick = 0.0
        self.storage_timer = QtCore.QTimer(self)
        self.storage_timer.timeout.connect(self.check_storage)
        self.storage_timer.start(STORAGE_CHECK_MS)
        QtCore.QTimer.singleShot(0, self.check_storage)
        QtCore.QTimer.singleShot(500, self.outputs_panel.refresh)
        if self.camera is not None and config.camera.auto_start:
            QtCore.QTimer.singleShot(200, self.camera.start)
        if auto_connect:
            if sim:
                self.select_port("SIM")
            QtCore.QTimer.singleShot(0, lambda: self.connect_machine(port, sim))

    # ------------------------------------------------------------------ layout --
    def _build(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        top = QtWidgets.QHBoxLayout()
        self.port_combo = narrow_combo(QtWidgets.QComboBox(), 16)   # long port descriptions show in the list
        refresh = button("⟳", self.refresh_ports)
        refresh.setFixedWidth(em(3))
        refresh.setToolTip("Rescan serial ports")
        self.connect_btn = button("Connect", self.toggle_connection)
        self.conn_label = QtWidgets.QLabel("Not connected")
        self.conn_label.setWordWrap(True)
        self.stop_btn = button("STOP", self.stop_all, stop_style())
        self.stop_btn.setMinimumSize(em(8), em(3.4))
        self.stop_btn.setToolTip("Stop the motors now (Esc or Space)")
        top.addWidget(self.port_combo)
        top.addWidget(refresh)
        top.addWidget(self.connect_btn)
        top.addWidget(self.conn_label, 1)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        readouts = QtWidgets.QHBoxLayout()
        self.load_label = self._readout(readouts, "LOAD")
        self.pos_label = self._readout(readouts, "POSITION")
        self.ext_label = self._readout(readouts, "EXTENSION")
        self.state_label = self._readout(readouts, "STATE")
        root.addLayout(readouts)

        self.banner = QtWidgets.QLabel()
        self.banner.setStyleSheet(banner_style())
        self.banner.setWordWrap(True)
        self.banner.hide()
        root.addWidget(self.banner)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.plots = LivePlots(self.config.gui.time_window_s, units=self.units)
        if self.camera is not None:
            from vsbc.gui.camera_view import CameraView

            self.camera_view = CameraView(self)
            self.plots.insertTab(0, self.camera_view, "Camera")      # the default tab
            self.plots.setCurrentIndex(0)
        splitter.addWidget(self.plots)
        self.tabs = FlowTabs()          # its tab buttons wrap onto a second row on a narrow screen
        self.test_panel = TestPanel(self)
        self.jog_panel = JogPanel(self)
        self.calibrate_panel = CalibratePanel(self)
        self.outputs_panel = OutputsPanel(self)
        self.units_panel = UnitsPanel(self)
        self.console = ConsolePanel(self)
        self.firmware_panel = FirmwarePanel(self)
        self.panels = [self.test_panel, self.jog_panel, self.calibrate_panel, self.outputs_panel, self.units_panel,
                       self.console, self.firmware_panel]
        self.tabs.addTab(self._scroll(self.test_panel), "Test")
        if self.camera is not None:
            from vsbc.gui.camera_panel import CameraPanel

            self.camera_panel = CameraPanel(self)
            self.panels.append(self.camera_panel)
            self.tabs.addTab(self._scroll(self.camera_panel), "Camera")
        self.outputs_index = self.tabs.addTab(self._scroll(self.outputs_panel), "Outputs")
        self.tabs.addTab(self._scroll(self.jog_panel), "Jog")
        self.tabs.addTab(self._scroll(self.calibrate_panel), "Calibrate")
        self.tabs.addTab(self._scroll(self.units_panel), "Units")
        self.tabs.addTab(self.console, "Console")
        self.tabs.addTab(self._scroll(self.firmware_panel), "Firmware")
        self.tabs.currentChanged.connect(self._tab_changed)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([3000, 2000])             # 3 : 2 of whatever width the screen has
        splitter.setChildrenCollapsible(False)      # a stray drag on the touchscreen must not hide a side
        splitter.setHandleWidth(em(0.6))
        # Wide enough for every tab's controls, so none of them scrolls sideways.
        self.tabs.setMinimumWidth(min(self._tabs_min_width(), int(0.6 * screen_size(self)[0])))
        root.addWidget(splitter, 1)
        self.statusBar()

        for key in ("Esc", "Space"):
            shortcut = QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self.stop_all)
        self.refresh_ports()

    @staticmethod
    def _readout(layout, caption: str) -> QtWidgets.QLabel:
        box = QtWidgets.QVBoxLayout()
        cap = QtWidgets.QLabel(caption)
        cap.setStyleSheet(caption_style())
        value = QtWidgets.QLabel("—")
        value.setStyleSheet(readout_style())
        box.addWidget(cap)
        box.addWidget(value)
        layout.addLayout(box, 1)
        return value

    @staticmethod
    def _scroll(widget) -> QtWidgets.QScrollArea:
        """Scrolls up and down when the screen is too short; never sideways (the tabs are wide enough)."""
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    def _tabs_min_width(self) -> int:
        """The widest tab's minimum width, plus room for a vertical scroll bar."""
        widest = 0
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            inner = page.widget() if isinstance(page, QtWidgets.QScrollArea) else page
            widest = max(widest, inner.minimumSizeHint().width())
        bar = self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent)
        return widest + bar + em(0.5)

    def _tab_changed(self, index: int) -> None:
        if index == self.outputs_index:
            self.outputs_panel.refresh()

    # ------------------------------------------------------- dialogs / status --
    def ask(self, title: str, text: str) -> bool:
        if not self.interactive:
            log.info("ask (answered yes automatically): %s", text.replace("\n", " "))
            return True
        buttons = QtWidgets.QMessageBox.StandardButton
        answer = QtWidgets.QMessageBox.question(self, title, text, buttons.Yes | buttons.No, buttons.No)
        return answer == buttons.Yes

    def inform(self, title: str, text: str) -> None:
        log.info("%s: %s", title, text.replace("\n", " "))
        if self.interactive:
            QtWidgets.QMessageBox.information(self, title, text)

    def warn(self, title: str, text: str) -> None:
        log.warning("%s: %s", title, text.replace("\n", " "))
        if self.interactive:
            QtWidgets.QMessageBox.warning(self, title, text)

    def status(self, text: str) -> None:
        self.statusBar().showMessage(text, 15000)

    def show_warnings(self, warnings: list[str]) -> None:
        self._warnings = list(warnings)
        self.banner.setText("   •   ".join(self._warnings))
        critical = any("almost full" in w or "stopped recording" in w for w in self._warnings)
        self.banner.setStyleSheet(banner_style(critical))
        self.banner.setVisible(bool(self._warnings))

    def _add_warning(self, text: str) -> None:
        if text not in self._warnings:
            self.show_warnings(self._warnings + [text])

    def _remove_warnings(self, containing: str) -> None:
        self.show_warnings([w for w in self._warnings if containing not in w])

    # ------------------------------------------------------------ async work --
    def run_async(self, fn, on_done=None, on_error=None, busy: bool = True) -> None:
        """Run fn() on a worker thread; on_done(result) / on_error(exc) run on the Qt thread."""
        if busy:
            self.busy_ops += 1

        def work():
            try:
                result = fn()
            except BaseException as e:  # noqa: BLE001 - reported on the Qt thread
                if not isinstance(e, (DeviceError, LinkClosed, TimeoutError, PreflightError)):
                    log.exception("background task failed")
                self.inbox.put(("done", busy, on_error or self._async_error, e))
            else:
                self.inbox.put(("done", busy, on_done, result))

        threading.Thread(target=work, daemon=True).start()

    def post(self, fn, *args) -> None:
        """Run fn(*args) on the Qt thread (safe to call from any thread)."""
        self.inbox.put(("call", fn, args))

    def _async_error(self, exc: BaseException) -> None:
        self.warn("Error", str(exc) or type(exc).__name__)

    def _on_message(self, msg) -> None:     # serial reader thread
        self.inbox.put(("msg", msg))

    def _on_raw(self, text: str, sent: bool) -> None:   # reader / writer threads
        self.inbox.put(("raw", text, sent))

    def _tick(self) -> None:
        for _ in range(5000):
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                kind = item[0]
                if kind == "msg":
                    self._handle_message(item[1])
                elif kind == "raw":
                    self.console.append(item[1], item[2])
                elif kind == "call":
                    item[1](*item[2])
                elif kind == "done":
                    _, busy, callback, value = item
                    if busy:
                        self.busy_ops = max(0, self.busy_ops - 1)
                    if callback:
                        callback(value)
            except Exception:  # noqa: BLE001 - keep the GUI alive
                log.exception("GUI update failed")
        self.plots.refresh()
        if self.camera is not None:
            try:
                self.camera_view.refresh()
            except Exception:  # noqa: BLE001
                log.exception("camera view update failed")
        now = time.monotonic()
        if now - self._last_slow_tick >= 0.5:
            self._last_slow_tick = now
            try:
                self.postprocess.poll()
                if self.camera is not None:
                    self.camera_panel.update_estimate()
            except Exception:  # noqa: BLE001
                log.exception("post-processing update failed")
        self._update_readouts()
        self._update_enabled()

    # -------------------------------------------------------------- messages --
    def _handle_message(self, msg) -> None:
        if isinstance(msg, Sample):
            ext = None
            if self.test_start_pos is not None and msg.state == "R":
                ext = msg.pos_mm - self.test_start_pos
                self.last_ext = ext
                self._check_tension(msg.load_n, ext)
            self.plots.add_sample(msg, ext)
        elif isinstance(msg, Reply):
            if msg.ok and msg.command == "RUN":
                self.test_start_pos = msg.number("from")
                self.last_ext = 0.0
                self._tension = {"count": 0, "max": -math.inf, "prev": -math.inf, "warned": False}
                self.plots.begin_test()
        elif isinstance(msg, Event):
            self._handle_event(msg)

    def _check_tension(self, load_n: float, ext_mm: float) -> None:
        """Pulling must read as positive load; say so at once when it does not."""
        t = self._tension
        if t["warned"] or not math.isfinite(load_n):
            return
        threshold = tension_threshold_n(self.config.loadcell.capacity_n)
        t["max"] = max(t["max"], load_n)
        if t["max"] > threshold:
            return
        rising = ext_mm >= t["prev"] - 1e-9
        t["prev"] = ext_mm
        t["count"] = t["count"] + 1 if load_n < -threshold and rising else 0
        if t["count"] >= 5:
            t["warned"] = True
            self._add_warning(TENSION_WARNING)

    def _handle_event(self, ev: Event) -> None:
        u = self.units
        if ev.name == "MOVE_END":
            reason = ev.fields.get("reason", "?")
            text = f"Move ended ({reason}) at {u.format_length(ev.number('pos'))}"
            if ev.fields.get("mode") == "RUN":
                self.test_start_pos = None
                peak = ev.number("peak")
                if math.isfinite(peak):
                    text += f", peak {u.format_force(peak)}"
            if reason in STOP_REASONS:
                text += f": {STOP_REASONS[reason]}"
            self.status(text)
            self.console.note(text)
            if reason in SAFETY_STOPS:
                self._add_warning(f"Last move stopped: {STOP_REASONS[reason]}.")
        elif ev.name == "LOADCELL":
            if ev.fields.get("state") == "LOST":
                self._add_warning("Load cell stopped responding.")
            else:
                # Not every "Load cell" warning: "Load cell not calibrated." still applies.
                self._remove_warnings("Load cell not responding")
                self._remove_warnings("Load cell stopped responding")
        elif ev.name == "OVERLOAD":
            self.status(f"Overload: {u.format_force(ev.number('load'), 1)}. Back the load off.")
        elif ev.name == "READY":
            if self._ready_seen and self.machine.connected:
                # The Mega reset while connected (power glitch, reset button): it forgot the
                # stream/watchdog settings and its position.
                self.console.note("The Mega reset.")
                self._add_warning("The Mega reset: set the position reference again (Jog tab).")
                self.run_async(self.machine.reconfigure_after_reset, self.calibrate_panel.show_settings, busy=False)
        elif ev.name == "DISCONNECTED":
            self._add_warning(f"Connection lost: {ev.fields.get('error', '')}")
            self.run_async(self.machine.disconnect, lambda _r: self.on_disconnected(),
                           lambda _e: self.on_disconnected(), busy=False)

    # ------------------------------------------------------------ connection --
    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        self.port_combo.addItem("Auto-detect", "AUTO")
        try:
            ports = list_ports()
        except Exception:  # noqa: BLE001 - no pyserial port backend
            ports = []
        for device, description in ports:
            self.port_combo.addItem(f"{device} — {description}" if description else device, device)
        self.port_combo.addItem("Simulator (no hardware)", "SIM")
        self.select_port(current or self.config.serial.port or "AUTO")

    def select_port(self, data: str) -> None:
        index = self.port_combo.findData(data)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)

    def selected_port(self) -> str | None:
        data = self.port_combo.currentData()
        return None if data in (None, "AUTO") else data

    @property
    def has_saved_position(self) -> bool:
        return self._saved_position

    def toggle_connection(self) -> None:
        if self.machine.connected:
            self.disconnect_machine()
        else:
            port = self.selected_port()
            self.connect_machine(None if port == "SIM" else port, sim=port == "SIM")

    def connect_machine(self, port: str | None = None, sim=False) -> None:
        if self._connecting:
            return
        self._connecting = True
        self._ready_seen = False
        self.conn_label.setText("Connecting...")

        def work():
            info = self.machine.connect(None if sim else port, sim=sim)
            if self.machine.sim is not None and not self.machine.status().referenced:
                self.machine.zero()   # the simulator starts unreferenced; its zero is as good as any
                self.machine.warnings = [w for w in self.machine.warnings if "referenced" not in w]
            return info

        self.run_async(work, self._on_connected, self._on_connect_failed, busy=False)

    def _on_connected(self, info) -> None:
        self._connecting = False
        self._ready_seen = True
        self.connect_btn.setText("Disconnect")
        where = "simulator" if info.simulated else self.machine.port
        self.conn_label.setText(f"{where} · {info.name} {info.version}")
        self._saved_position = self.machine.saved_position() is not None
        for panel in self.panels:
            panel.on_connected()
        self.show_warnings(self.machine.warnings)
        self.check_storage()
        self.console.note(f"connected to {where}: {info.name} {info.version} (protocol {info.protocol})")
        self.status(f"Connected to {where}")
        if not info.simulated and self._saved_position and any("referenced" in w for w in self.machine.warnings):
            self.jog_panel.restore()   # asks first

    def _on_connect_failed(self, exc: BaseException) -> None:
        self._connecting = False
        self.conn_label.setText("Not connected")
        text = str(exc) or type(exc).__name__
        if isinstance(exc, FirmwareMismatch):
            if exc.banner:
                text += "\n\nThe board printed:\n" + "\n".join(exc.banner[:6])
            text += "\n\nThe Firmware tab can upload the right firmware."
            self.tabs.setCurrentWidget(self.firmware_panel)
        self.warn("Could not connect", text)

    def disconnect_machine(self) -> None:
        if self.runner is not None:
            if not self.ask("Disconnect", "A test is running. Stop it and disconnect?"):
                return
            self.runner.request_stop()
        self._connecting = True
        self.run_async(self.machine.disconnect, lambda _r: self.on_disconnected(),
                       lambda _e: self.on_disconnected(), busy=False)

    def on_disconnected(self) -> None:
        self._connecting = False
        self._ready_seen = False
        self._saved_position = self.machine.saved_position() is not None
        self.connect_btn.setText("Connect")
        self.conn_label.setText("Not connected")
        self._remove_warnings("referenced")
        self.firmware_panel.update_info()
        self.status("Disconnected")

    # ------------------------------------------------------------------ STOP --
    def stop_all(self) -> None:
        if self.runner is not None:
            self.runner.request_stop()
        if self.machine.connected:
            try:
                self.machine.stop()
            except LinkClosed:
                pass
            self.status("STOP sent")

    # ------------------------------------------------------------------ test --
    def start_test(self) -> None:
        if self.runner is not None:
            return
        spec = self.test_panel.build_spec()
        before_run = after_run = None
        camera_warnings: list[str] = []
        recording = self.camera is not None and self.camera_panel.record_with_test.isChecked()
        if recording:
            errors, camera_warnings, recording = self.camera_panel.preflight()
            if errors:
                self.warn("Cannot start the test", "\n\n".join(errors))
                return
        if recording:
            info = self.camera.info
            roi = self.roi if self.roi is not None and self.roi.matches(info.get("height", 0), info.get("width", 0)) \
                else None
            before_run, after_run = self.camera.test_hooks(
                self.camera_panel.recording_options(spec.specimen_id), roi, self.roi_snapshot if roi else None,
                on_settle=lambda seconds: self.post(self.camera_view.show_countdown, seconds))
        runner = PullTestRunner(self.machine, spec, self.config, before_run=before_run, after_run=after_run,
                                units=self.units)
        try:
            warnings = runner.preflight() + camera_warnings
        except (PreflightError, DeviceError, LinkClosed, TimeoutError) as e:
            self.warn("Cannot start the test", str(e))
            return
        if warnings and not self.ask("Start the test?", "\n\n".join(warnings) + "\n\nStart anyway?"):
            return
        self.runner = runner
        if self.camera is not None:
            self.camera.runner = runner if recording else None
        if self.postprocess.busy and self.postprocess.pause():
            self.console.note("Post-processing paused during the test.")
        self.last_result = None
        self.last_ext = None
        self._remove_warnings("camera stopped recording")
        self.test_panel.show_running(spec)
        self.plots.begin_test()
        self.run_async(runner.run, self._test_finished, self._test_failed)

    def _test_finished(self, result) -> None:
        self.runner = None
        if self.camera is not None:
            self.camera.runner = None
        self.last_result = result
        self.test_panel.show_result(result)
        s = result.summary
        if s is not None and s.n_samples:
            self.plots.mark_peak(s.extension_at_peak_mm, s.peak_load_n)
        if any("sign is reversed" in w for w in result.warnings):
            self._add_warning(TENSION_WARNING)
        self.status(f"Test finished ({result.stop_reason}). Files: {result.folder}")
        if result.folder is not None and (result.folder / "camera").is_dir() and self.camera is not None:
            self.last_camera_folder = result.folder / "camera"
            self._pending_sync = result.folder
            if self.camera.recording is None:          # the camera already finished writing
                self._sync_test(result.folder)
        else:
            self.postprocess.resume()
        self.check_storage()

    def _test_failed(self, exc: BaseException) -> None:
        self.runner = None
        if self.camera is not None:
            self.camera.runner = None
        self.postprocess.resume()
        self.warn("Test failed", str(exc))

    # ---------------------------------------------------------------- camera --
    def _on_camera_event(self, msg: dict) -> None:
        """Camera process events, on the Qt thread."""
        ev = msg.get("ev")
        self.camera_view.on_event(msg)
        self.camera_panel.on_event(msg)
        if ev == "recording":
            self.outputs_panel.copy_pause.set()
        elif ev == "high_speed":
            self.test_panel.show_high_speed(bool(msg.get("on")))
            self.console.note(f"Camera: full-rate saving {'on' if msg.get('on') else 'off'}"
                              + (f" ({msg.get('label')})" if msg.get("label") else ""))
        elif ev == "finished":
            self.outputs_panel.copy_pause.clear()
            self.test_panel.show_high_speed(False)
            summary = msg.get("summary") or {}
            if summary.get("failed"):
                self._add_warning(f"The camera stopped recording: {summary['failed']}.")
            self.console.note(f"Camera: {summary.get('frames_saved', 0)} of {summary.get('frames_grabbed', 0)} "
                              f"frames saved ({summary.get('frames_matched', 0)} paired with load samples)")
            if self._pending_sync is not None and self.runner is None:
                self._sync_test(self._pending_sync)
            elif self._manual_folder is not None:
                folder, self._manual_folder = self._manual_folder, None
                self.last_camera_folder = folder
                self._sync_recording(folder)
            self.check_storage()
        elif ev == "stopped_itself":
            self._add_warning(f"The camera stopped recording: {msg.get('reason')}"
                              + (". The test was stopped with it." if self.runner is not None else "."))
        elif ev == "lost" and not msg.get("expected"):
            self._add_warning(f"Camera: {msg.get('message') or 'the camera process stopped'}")
        elif ev == "error" and msg.get("fatal") and msg.get("where") == "camera":
            self._add_warning(msg.get("message") or "Camera error")
        elif ev == "opened":
            self._remove_warnings("Camera")

    def set_high_speed(self, on: bool) -> None:
        cam = self.camera
        if cam is None or cam.recording is None:
            self.test_panel.show_high_speed(False)
            if cam is not None:
                self.camera_view.show_high_speed(False)
            return
        cam.send("high_speed", on=bool(on))
        self.test_panel.show_high_speed(on)
        self.camera_view.show_high_speed(on)

    def start_manual_recording(self, name: str) -> None:
        cam = self.camera
        if cam is None or cam.state != "preview":
            self.warn("Record", "The camera is not ready.")
            return
        info = cam.info
        roi = self.roi if self.roi is not None and self.roi.matches(info.get("height", 0), info.get("width", 0)) \
            else None
        options = self.camera_panel.recording_options(name)
        options["max_s"] = float(self.script_settings().get("MAX_SECONDS") or 45.0)
        folder = cam.manual_folder(name)
        self._manual_folder = folder
        settle = float(options.get("settle_s") or 0.0)
        self.camera_view.show_countdown(settle)

        def work():
            time.sleep(settle)
            cam.begin_recording(folder, options, roi, self.roi_snapshot if roi else None, test=False)

        def failed(exc):
            self._manual_folder = None
            self.warn("Record", str(exc))

        self.run_async(work, lambda _r: self.status(f"Recording to {folder}"), failed, busy=False)

    def stop_camera_recording(self) -> None:
        cam = self.camera
        rec = cam.recording if cam is not None else None
        if rec is None:
            return
        if rec.get("test") and self.runner is not None:
            if not self.ask("Stop recording", "The camera and the test stop together: this stops the test too. "
                                              "Stop both?"):
                return
            self.runner.request_stop("the camera recording was stopped from the Camera tab")
            return
        cam.stop_recording("stopped from the Camera tab")

    def script_settings(self) -> dict:
        """capture_dic_live.py's USER SETTINGS: as the camera process loaded them, else read from the file."""
        from vsbc.camera.scripts import literal_settings

        settings = self.camera.hello.get("settings") if self.camera is not None else None
        return settings or literal_settings(self.config.camera_dir)

    def _load_roi(self) -> None:
        from vsbc.camera import tiff
        from vsbc.camera.roi import load_roi

        self.roi = load_roi(self.roi_file)
        try:
            self.roi_snapshot = tiff.read(self.roi_snapshot_file) if self.roi_snapshot_file.is_file() else None
        except (OSError, ValueError, ImportError):
            self.roi_snapshot = None

    @property
    def roi_file(self) -> Path:
        return self.config.camera_recordings_dir / "last_roi.npz"

    @property
    def roi_snapshot_file(self) -> Path:
        return self.config.camera_recordings_dir / "last_roi_snapshot.tif"

    def set_roi(self, roi, snapshot) -> None:
        from vsbc.camera import tiff
        from vsbc.camera.roi import save_roi

        save_roi(self.roi_file, roi)
        if snapshot is not None:
            tiff.write_gray8(self.roi_snapshot_file, snapshot)
        self.roi, self.roi_snapshot = roi, snapshot
        self.camera_view.draw_roi()

    # ------------------------------------------------------- post-processing --
    def _sync_test(self, test_folder: Path) -> None:
        """After a test with the camera: put the frames on data.csv's clock, then queue the jobs."""
        from vsbc.camera.sync import sync_test

        self._pending_sync = None
        delay = self.camera_panel.load_delay.value()
        options = self.camera_panel.post_options()
        settings = self.script_settings()

        def work():
            summary = sync_test(test_folder, delay)
            return summary, self.postprocess.jobs_for(test_folder / "camera", test_folder, options, settings)

        def done(result):
            summary, jobs = result
            self.postprocess.add(jobs)
            self.postprocess.resume()
            self._show_sync(summary, options.notes)

        def failed(exc):
            self.postprocess.resume()
            log.exception("camera sync failed", exc_info=exc)
            self.warn("Camera", f"Could not line the camera frames up with data.csv: {exc}")

        self.run_async(work, done, failed, busy=False)

    def _sync_recording(self, folder: Path) -> None:
        from vsbc.camera.sync import sync_camera

        delay = self.camera_panel.load_delay.value()
        options = self.camera_panel.post_options()
        settings = self.script_settings()

        def work():
            summary = sync_camera(folder, None, {}, delay)
            return summary, self.postprocess.jobs_for(folder, None, options, settings)

        def done(result):
            summary, jobs = result
            self.postprocess.add(jobs)
            self._show_sync(summary, options.notes)

        self.run_async(work, done, lambda exc: self.warn("Camera", f"Sync failed: {exc}"), busy=False)

    def _show_sync(self, summary: dict, notes: list[str]) -> None:
        pairing = summary.get("data_csv_pairing") or {}
        text = f"Camera: {summary.get('frames_saved', 0)} frames saved"
        if pairing:
            text += (f"; each data.csv row has a frame within {pairing['frame_dt_ms_p95']:.1f} ms "
                     f"(95 %), {pairing['frame_dt_ms_max']:.1f} ms at most")
        self.console.note(text)
        for note in notes:
            self.console.note(f"Camera: {note}")
        if self.last_result is not None and self.last_result.folder is not None:
            extra = "\n\n" + text + "".join(f"\n{n}" for n in notes)
            self.test_panel.result.appendPlainText(extra)
        self.status(text)

    def queue_postprocess(self, folder: Path) -> None:
        """Process a folder: a test folder, its camera/ folder, or a recording made from the Camera tab."""
        from vsbc.camera.roi import load_roi, save_roi
        from vsbc.camera.sync import read_json, sync_camera, sync_test

        folder = Path(folder)
        if (folder / "camera" / "frames.csv").is_file():
            test, run = folder, folder / "camera"
        elif (folder / "frames.csv").is_file():
            run = folder
            test = folder.parent if folder.name == "camera" and (folder.parent / "data.csv").is_file() else None
        else:
            self.warn("Process", f"{folder} has no camera recording (frames.csv).")
            return
        if load_roi(run / "roi.npz") is None and self.roi is not None:
            settings = read_json(run / "capture_settings.json")
            if self.roi.matches(settings.get("height", 0), settings.get("width", 0)) and self.ask(
                    "Process", "This recording has no ROI. Use the current ROI (Camera tab) for it?"):
                save_roi(run / "roi.npz", self.roi)
        options = self.camera_panel.post_options() if self.camera is not None else None
        if options is None:
            return
        delay = self.camera_panel.load_delay.value()
        settings = self.script_settings()
        synced = (run / "samples.csv").is_file() or (run / "camera_clock.csv").is_file()

        def work():
            if synced:
                if test is not None:
                    sync_test(test, delay)
                else:
                    sync_camera(run, None, {}, delay)
            return self.postprocess.jobs_for(run, test, options, settings)

        def done(jobs):
            self.postprocess.add(jobs)
            self.status(f"Queued {len(jobs)} job(s) for {folder.name}")
            for note in options.notes:
                self.console.note(f"Camera: {note}")

        self.run_async(work, done, busy=False)

    def run_camera_check(self) -> None:
        """`vsbc camera check` from the GUI: the camera process lets go of the camera meanwhile."""
        from vsbc.camera.client import camera_env, camera_python
        from vsbc.camera.jobs import format_check
        from vsbc.camera.sync import read_json

        cam = self.camera
        if cam.recording is not None:
            self.warn("Check", "Not while recording.")
            return
        if not self.ask("Check", "The check stops the camera preview for about half a minute: it grabs at full "
                                 "rate, measures the disk and runs a small DIC. Run it?"):
            return
        cfg = self.config

        def work():
            was_running = cam.state != "off"
            cam.stop()
            args = [camera_python(cfg), "-m", "vsbc.camera.jobs", "check", "--camera-dir", str(cfg.camera_dir),
                    "--output-dir", str(cfg.output_dir)] + (["--sim"] if cam.sim else [])
            try:
                done = subprocess.run(args, env=camera_env(), cwd=str(REPO_ROOT), capture_output=True, text=True,
                                      timeout=600)
            finally:
                if was_running:
                    cam.start()
            return read_json(cfg.output_dir / "camera_check.json"), done.stderr[-1500:]

        def done(result):
            report, errors = result
            self.inform("Camera check", format_check(report) + (f"\n\n{errors}" if errors and not report else ""))

        self.run_async(work, done, busy=False)

    def check_storage(self) -> None:
        try:
            status = disk_status(self.config.output_dir, self.config.storage.warn_free_gb,
                                 self.config.storage.critical_free_gb)
        except OSError:
            return
        self.outputs_panel.show_disk(status)
        self._remove_warnings("Storage")
        if status.level != "ok":
            what = "almost full" if status.level == "critical" else "low"
            self._add_warning(f"Storage {what}: {human_size(status.free)} free on this computer "
                              "(Outputs tab: copy to USB, delete camera images).")
        self.tabs.setTabText(self.outputs_index, "Outputs" if status.level == "ok" else "Outputs ⚠")

    # ------------------------------------------------------------------ units --
    def set_units(self, units: Units, remember: bool = True) -> None:
        self.units = units
        if remember:
            save_units(self.config, units)
        else:
            clear_saved_units(self.config)
        self.plots.apply_units(units)
        for panel in self.panels:
            panel.apply_units(units)
        self.status(f"Units: {units.force}, {units.length}")

    # ------------------------------------------------------------- refreshing --
    def _update_readouts(self) -> None:
        connected = self.machine.connected
        s = self.machine.latest if connected else None
        u = self.units
        self.load_label.setText(u.format_force(s.load_n) if s else "—")
        self.pos_label.setText(u.format_length(s.pos_mm) if s else "—")
        self.ext_label.setText(u.format_length(self.last_ext) if self.last_ext is not None else "—")
        if not connected:
            state = "OFFLINE"
        elif self.runner is not None:
            state = "TEST"
        else:
            state = STATE_NAMES.get(s.state, s.state) if s else "—"
        self.state_label.setText(state)

    def _update_enabled(self) -> None:
        connected = self.machine.connected
        s = self.machine.latest
        busy = self.busy_ops > 0 or self.runner is not None or (connected and s is not None and s.moving)
        for panel in self.panels:
            panel.update_enabled(connected, busy)
        self.port_combo.setEnabled(not connected and not self._connecting)
        self.connect_btn.setEnabled(not self._connecting and self.runner is None)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt name
        if self.runner is not None:
            if not self.ask("Quit", "A test is running. Stop it and quit?"):
                event.ignore()
                return
            self.runner.request_stop()
        cam = self.camera
        if cam is not None and cam.recording is not None:
            if not self.ask("Quit", "The camera is recording. Stop it and quit? (The frames so far are kept.)"):
                event.ignore()
                return
            cam.stop_recording("the app closed")
            cam.wait_finished(60)
        if self.postprocess.busy and not self.ask(
                "Quit", "Post-processing is still running. Quit anyway? It stops; run it again later with "
                        "Camera > Process a folder."):
            event.ignore()
            return
        self.timer.stop()
        self.storage_timer.stop()
        self.postprocess.stop()
        if cam is not None:
            cam.stop()
        try:
            self.machine.disconnect()
        except Exception:  # noqa: BLE001
            log.exception("disconnect on exit failed")
        event.accept()


def run_gui(config: Config, port: str | None = None, sim: bool = False, fullscreen: bool = False) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("VSBC Tensile Tester")
    font_px = apply_style(app, config.gui.font_pt, config.gui.scale)
    width, height = screen_size()
    log.info("screen %d × %d px: text %.1f px", width, height, font_px)
    win = MainWindow(config, port=port, sim=sim, auto_connect=bool(port or sim))
    if fullscreen:
        win.showFullScreen()
    elif width < 1300 or height < 860:          # 1280 × 800 and the title bar would not fit
        win.showMaximized()
    else:
        win.resize(1280, 800)
        win.show()
    return exec_app(app)
