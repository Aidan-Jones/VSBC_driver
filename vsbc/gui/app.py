"""Main window: connection bar, STOP, live readouts, plots and the tabs.

Threading: the serial reader thread and worker threads never touch widgets.
They put items into `self.inbox`, and a QTimer on the Qt thread drains it
(about 25 times a second), updating the plots and readouts and running callbacks.
"""

from __future__ import annotations

import logging
import math
import queue
import sys
import threading

from vsbc.config import Config
from vsbc.gui.calibration_panel import CalibratePanel
from vsbc.gui.common import (
    BANNER_STYLE, CAPTION_STYLE, READOUT_STYLE, STOP_STYLE, QShortcut, Qt, QtCore, QtGui, QtWidgets,
    apply_style, button, exec_app, fmt,
)
from vsbc.gui.panels import ConsolePanel, FirmwarePanel, JogPanel, TestPanel
from vsbc.gui.plots import LivePlots
from vsbc.link import DeviceError, LinkClosed, list_ports
from vsbc.machine import FirmwareMismatch, Machine
from vsbc.procedures import PreflightError, PullTestRunner
from vsbc.protocol import STATE_NAMES, Event, Reply, Sample

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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config: Config, *, port: str | None = None, sim=False,
                 auto_connect: bool = False, interactive: bool = True):
        super().__init__()
        self.config = config
        self.interactive = interactive          # False in tests: dialogs answer "yes" by themselves
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

        self.setWindowTitle("VSBC Tensile Tester")
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(max(10, int(1000 / config.gui.refresh_hz)))
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
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(240)
        refresh = button("⟳", self.refresh_ports)
        refresh.setFixedWidth(48)
        refresh.setToolTip("Rescan serial ports")
        self.connect_btn = button("Connect", self.toggle_connection)
        self.conn_label = QtWidgets.QLabel("Not connected")
        self.stop_btn = button("STOP", self.stop_all, STOP_STYLE)
        self.stop_btn.setMinimumSize(170, 64)
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
        self.banner.setStyleSheet(BANNER_STYLE)
        self.banner.setWordWrap(True)
        self.banner.hide()
        root.addWidget(self.banner)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.plots = LivePlots(self.config.gui.time_window_s)
        splitter.addWidget(self.plots)
        self.tabs = QtWidgets.QTabWidget()
        self.test_panel = TestPanel(self)
        self.jog_panel = JogPanel(self)
        self.calibrate_panel = CalibratePanel(self)
        self.console = ConsolePanel(self)
        self.firmware_panel = FirmwarePanel(self)
        self.panels = [self.test_panel, self.jog_panel, self.calibrate_panel, self.console, self.firmware_panel]
        self.tabs.addTab(self._scroll(self.test_panel), "Test")
        self.tabs.addTab(self._scroll(self.jog_panel), "Jog")
        self.tabs.addTab(self._scroll(self.calibrate_panel), "Calibrate")
        self.tabs.addTab(self.console, "Console")
        self.tabs.addTab(self.firmware_panel, "Firmware")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
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
        cap.setStyleSheet(CAPTION_STYLE)
        value = QtWidgets.QLabel("—")
        value.setStyleSheet(READOUT_STYLE)
        box.addWidget(cap)
        box.addWidget(value)
        layout.addLayout(box, 1)
        return value

    @staticmethod
    def _scroll(widget) -> QtWidgets.QScrollArea:
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        area.setWidget(widget)
        return area

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
        self._update_readouts()
        self._update_enabled()

    # -------------------------------------------------------------- messages --
    def _handle_message(self, msg) -> None:
        if isinstance(msg, Sample):
            ext = None
            if self.test_start_pos is not None and msg.state == "R":
                ext = msg.pos_mm - self.test_start_pos
                self.last_ext = ext
            self.plots.add_sample(msg, ext)
        elif isinstance(msg, Reply):
            if msg.ok and msg.command == "RUN":
                self.test_start_pos = msg.number("from")
                self.last_ext = 0.0
                self.plots.begin_test()
        elif isinstance(msg, Event):
            self._handle_event(msg)

    def _handle_event(self, ev: Event) -> None:
        if ev.name == "MOVE_END":
            reason = ev.fields.get("reason", "?")
            text = f"Move ended ({reason}) at {ev.number('pos'):.4f} mm"
            if ev.fields.get("mode") == "RUN":
                self.test_start_pos = None
                peak = ev.number("peak")
                if math.isfinite(peak):
                    text += f", peak {peak:.2f} N"
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
                self._remove_warnings("Load cell")
        elif ev.name == "OVERLOAD":
            self.status(f"Overload: {ev.number('load'):.1f} N. Back the load off.")
        elif ev.name == "READY":
            if self._ready_seen and self.machine.connected:
                # The Mega reset while connected (power glitch, reset button): it forgot the
                # stream/watchdog settings and its position.
                self.console.note("The Mega reset.")
                self._add_warning("The Mega reset: set the position reference again (Jog tab).")
                self.run_async(self._reconfigure_after_reset, self.calibrate_panel.show_settings, busy=False)
        elif ev.name == "DISCONNECTED":
            self._add_warning(f"Connection lost: {ev.fields.get('error', '')}")
            self.run_async(self.machine.disconnect, lambda _r: self.on_disconnected(),
                           lambda _e: self.on_disconnected(), busy=False)

    def _reconfigure_after_reset(self) -> dict:
        self.machine.set("watchdog_ms", self.config.watchdog_ms)
        self.machine.stream(True)
        return self.machine.refresh_settings()

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
        runner = PullTestRunner(self.machine, spec, self.config)
        try:
            warnings = runner.preflight()
        except (PreflightError, DeviceError, LinkClosed, TimeoutError) as e:
            self.warn("Cannot start the test", str(e))
            return
        if warnings and not self.ask("Start the test?", "\n\n".join(warnings) + "\n\nStart anyway?"):
            return
        self.runner = runner
        self.last_result = None
        self.last_ext = None
        self.test_panel.show_running(spec)
        self.plots.begin_test()
        self.run_async(runner.run, self._test_finished, self._test_failed)

    def _test_finished(self, result) -> None:
        self.runner = None
        self.last_result = result
        self.test_panel.show_result(result)
        s = result.summary
        if s is not None and s.n_samples:
            self.plots.mark_peak(s.extension_at_peak_mm, s.peak_load_n)
        self.status(f"Test finished ({result.stop_reason}). Files: {result.folder}")

    def _test_failed(self, exc: BaseException) -> None:
        self.runner = None
        self.warn("Test failed", str(exc))

    # ------------------------------------------------------------- refreshing --
    def _update_readouts(self) -> None:
        connected = self.machine.connected
        s = self.machine.latest if connected else None
        self.load_label.setText(fmt(s.load_n, 2, "N") if s else "—")
        self.pos_label.setText(fmt(s.pos_mm, 4, "mm") if s else "—")
        self.ext_label.setText(fmt(self.last_ext, 4, "mm") if self.last_ext is not None else "—")
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
        self.timer.stop()
        try:
            self.machine.disconnect()
        except Exception:  # noqa: BLE001
            log.exception("disconnect on exit failed")
        event.accept()


def run_gui(config: Config, port: str | None = None, sim: bool = False, fullscreen: bool = False) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("VSBC Tensile Tester")
    apply_style(app, config.gui.font_pt)
    win = MainWindow(config, port=port, sim=sim, auto_connect=bool(port or sim))
    if fullscreen:
        win.showFullScreen()
    else:
        win.resize(1280, 800)
        win.show()
    return exec_app(app)
