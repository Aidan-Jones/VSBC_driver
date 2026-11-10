"""Main window: connection bar, STOP, live readouts, plots and the tabs.

Threading: the serial reader thread and worker threads never touch widgets.
They put items into `self.inbox`, and a QTimer on the Qt thread drains it
(about 25 times a second), updating the plots and readouts and running callbacks.
The camera process's events arrive the same way (CameraClient -> post).
"""

from __future__ import annotations

import html
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
    SAFETY_STOPS, FlowTabs, QShortcut, Qt, QtCore, QtGui, QtWidgets, Section, StatCard, apply_style, banner_style,
    button, dot, em, exec_app, level_color, narrow_combo, pill_style, reveal, screen_size, set_role, state_color,
    stop_style, theme, themed, touch_scroll, touch_scroll_wanted,
)
from vsbc.gui.common import set_theme as set_app_theme
from vsbc.gui.outputs_panel import OutputsPanel
from vsbc.gui.panels import ConsolePanel, FirmwarePanel, JogPanel, TestPanel
from vsbc.gui.machine_panel import MachinePanel, SystemPanel
from vsbc.gui.plots import LivePlots
from vsbc.gui.readiness import readiness
from vsbc.gui.results_view import ResultsTab
from vsbc.gui.units_panel import UnitsPanel
from vsbc.link import DeviceError, LinkClosed, list_ports
from vsbc.machine import FirmwareMismatch, Machine
from vsbc.procedures import PreflightError, PullTestRunner
from vsbc.protocol import STATE_NAMES, Event, Reply, Sample
from vsbc.storage import disk_status, human_size
from vsbc.units import Units, clear_saved_units, effective_units, load_preferences, save_preferences, save_units

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
STORAGE_CHECK_MS = 30_000
# Warnings that stay in the banner (and colour it red) until their cause is gone: the × does not hide them.
CRITICAL_WARNINGS = ("almost full", "stopped recording", "Connection lost", "Load cell stopped responding")
CAMERA_STATE_COLORS = {"preview": "success", "recording": "danger", "finishing": "warning", "starting": "warning",
                       "error": "danger"}
STATUS_COLORS = {"info": "text", "success": "success", "warning": "warning", "danger": "danger"}
# Replies after which the Mega's state may have changed: read STATUS again for the Ready checklist.
STATUS_AFTER = {"ZERO", "TARE", "CAL", "MOTORS", "SET", "SAVE", "DEFAULTS"}
STATUS_POLL_S = 2.0


def is_critical(warning: str) -> bool:
    return any(word in warning for word in CRITICAL_WARNINGS)
TENSION_WARNING = ("Tension is reading negative: the load-cell sign is reversed (Machine tab > Calibration > Flip load sign). "
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
        self._dismissed: set[str] = set()       # warnings hidden with the banner's ×
        self._conn = ("Not connected", "muted")
        self._shown_state: str | None = None
        self.disabled_reason = "Connect to the machine first (Connect, top left)."
        self.readiness_items: list = []         # the Ready checklist (vsbc.gui.readiness), on the slow tick
        self._status_due = False                # read STATUS soon (after ZERO, TARE, ...)
        self._status_polling = False
        self._last_status_poll = 0.0
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
        self._restore_layout()
        self._restore_sections()
        # Touch screen: drag a page anywhere to scroll it (the scroll bars stay).
        self.touch = touch_scroll_wanted(config.gui.touch_scroll)
        if self.touch:
            areas = touch_scroll(self)
            # A drag scrolls the list now, and Ctrl / Shift do not exist on a touch screen: a tap adds a
            # test to the selection or takes it out.
            table = self.outputs_panel.table
            table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.MultiSelection)
            table.setToolTip("Tap tests to select them (tap again to unselect); double-tap one to open it in Results")
            log.info("touch scrolling on %d areas", areas)
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
        self.connect_btn = button("Connect", self.toggle_connection, role="primary")
        self.conn_label = QtWidgets.QLabel()
        self.conn_label.setWordWrap(True)
        self.conn_label.setTextFormat(Qt.TextFormat.RichText)
        self._show_connection(*self._conn)
        self.stop_btn = button("STOP", self.stop_all, stop_style)
        self.stop_btn.setMinimumSize(em(8), em(3.4))
        self.stop_btn.setToolTip("Stop the motors now (Esc or Space)")
        top.addWidget(self.port_combo)
        top.addWidget(refresh)
        top.addWidget(self.connect_btn)
        top.addWidget(self.conn_label, 1)
        # "✓ Ready" or "2 steps before a test": opens the checklist on the Test tab.
        self.ready_chip = button("", self.show_checklist, tip="What is still to do before a test")
        self.ready_chip.hide()
        top.addWidget(self.ready_chip)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        # Readout cards: the top edge has the colour of the matching plot curve.
        readouts = QtWidgets.QHBoxLayout()
        readouts.setSpacing(em(0.5))
        self.load_label = self._readout(readouts, "Load", "load")
        self.pos_label = self._readout(readouts, "Position", "position")
        self.ext_label = self._readout(readouts, "Extension", "extension")
        self.state_card = StatCard("State", "muted")
        self.state_label = self.state_card.value
        self.state_card.layout().setAlignment(self.state_label, Qt.AlignmentFlag.AlignLeft)
        readouts.addWidget(self.state_card, 1)
        root.addLayout(readouts)

        self.banner = QtWidgets.QFrame()
        self.banner.setObjectName("banner")
        self.banner_text = QtWidgets.QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_text.setTextFormat(Qt.TextFormat.RichText)
        self.banner_close = button("×", self.dismiss_warnings,
                                   tip="Hide these warnings. They come back if they happen again; "
                                       "red ones stay until fixed.")
        self.banner_close.setFixedWidth(em(2.4))
        banner_row = QtWidgets.QHBoxLayout(self.banner)
        banner_row.setContentsMargins(em(0.6), em(0.25), em(0.2), em(0.25))
        banner_row.addWidget(self.banner_text, 1)
        banner_row.addWidget(self.banner_close, 0, Qt.AlignmentFlag.AlignTop)
        themed(self.banner, banner_style)
        self.banner.hide()
        root.addWidget(self.banner)

        self.splitter = splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        # Left: Camera (the default) · Live (the two live plots) · Results (Overview and DIC analysis of a test).
        self.left_tabs = FlowTabs()
        if self.camera is not None:
            from vsbc.gui.camera_view import CameraView

            self.camera_view = CameraView(self)
            self.left_tabs.addTab(self.camera_view, "Camera")
        self.plots = LivePlots(self.config.gui.time_window_s, units=self.units)
        self.left_tabs.addTab(self.plots, "Live")
        # Not during a test: reading frames would compete with the camera for the disk.
        self.results_tab = ResultsTab(self)
        self.results_view, self.dic_view = self.results_tab.overview, self.results_tab.dic
        self.results_index = self.left_tabs.addTab(self.results_tab, "Results")
        self.left_tabs.setCurrentIndex(0)
        self._results_available = True
        self.left_tabs.currentChanged.connect(self._plot_tab_changed)
        splitter.addWidget(self.left_tabs)
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
        # Right, in the order of the work: Test · Machine · Camera · Outputs · System.
        self.machine_panel = MachinePanel(self)
        self.system_panel = SystemPanel(self)
        self.tabs.addTab(self._scroll(self.test_panel), "Test")
        self.tabs.addTab(self._scroll(self.machine_panel), "Machine")
        if self.camera is not None:
            from vsbc.gui.camera_panel import CameraPanel

            self.camera_panel = CameraPanel(self)
            self.panels.append(self.camera_panel)
            self.tabs.addTab(self._scroll(self.camera_panel), "Camera")
        self.outputs_index = self.tabs.addTab(self._scroll(self.outputs_panel), "Outputs")
        self.tabs.addTab(self.system_panel, "System")
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

        # Status bar, right side: post-processing, camera and disk at a glance.
        self.post_status = QtWidgets.QLabel()
        self.camera_status = QtWidgets.QLabel()
        self.disk_status = QtWidgets.QLabel()
        for label in (self.post_status, self.camera_status, self.disk_status):
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setContentsMargins(em(0.5), 0, em(0.5), 0)
            self.statusBar().addPermanentWidget(label)
        self.post_status.linkActivated.connect(lambda _link: self.show_dic())
        self.post_status.setToolTip("Open Results > DIC analysis")
        self.post_status.hide()
        self.camera_status.setVisible(self.camera is not None)

        for key in ("Esc", "Space"):
            shortcut = QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self.stop_all)
        self.refresh_ports()

    @staticmethod
    def _readout(layout, caption: str, color: str) -> QtWidgets.QLabel:
        card = StatCard(caption, color)
        layout.addWidget(card, 1)
        return card.value

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

    def _plot_tab_changed(self, index: int) -> None:
        if index == self.results_index:
            self.results_tab.on_shown()

    def show_results(self, folder: Path) -> None:
        """Open a test (or camera recording) in Results > Overview."""
        if not self._results_available:
            self.warn("Results", "The Results tab is not available while a test or a camera recording runs.")
            return
        for tabs, page in ((self.left_tabs, self.results_tab), (self.results_tab.inner, self.results_view)):
            tabs.blockSignals(True)              # show_folder loads it; on_shown need not load it again
            tabs.setCurrentWidget(page)
            tabs.blockSignals(False)
        self.results_view.show_folder(Path(folder))

    def show_dic(self, folder: Path | None = None) -> None:
        """Open Results > DIC analysis (for `folder`, else the test chosen there)."""
        if not self._results_available:
            self.warn("Results", "The Results tab is not available while a test or a camera recording runs.")
            return
        if folder is not None:
            self.show_results(folder)
        reveal(self.dic_view, glow=False)

    def reveal(self, widget) -> None:
        """Bring a control into view wherever it is (tabs, folded sections, scrolling) and flash it."""
        if widget is self.dic_view or self.dic_view.isAncestorOf(widget):
            if not self._results_available:
                return
        reveal(widget)

    def show_checklist(self) -> None:
        reveal(self.test_panel.ready)

    # ------------------------------------------------------------- readiness --
    def refresh_readiness(self) -> None:
        """The Ready checklist (Test tab), Start's hint and the top bar's chip."""
        try:
            items = readiness(self)
        except Exception:  # noqa: BLE001 - never break the window over the checklist
            log.exception("readiness check failed")
            return
        self.readiness_items = items
        self.test_panel.ready.show_items(items)
        todo = sum(1 for i in items if i.required)
        if not self.machine.connected:
            self.ready_chip.hide()
            return
        state = "warning" if todo else "success"
        self.ready_chip.setText(f"●  {todo} step{'s' if todo > 1 else ''} before a test" if todo else "✓  Ready")
        themed(self.ready_chip, lambda c=state: (
            f"QPushButton {{ color: {getattr(theme(), c)}; border: 2px solid {getattr(theme(), c)};"
            f" font-weight: bold; border-radius: {em(1.2)}px; padding: 0 {em(0.8)}px; }}"))
        self.ready_chip.show()

    def poll_status(self) -> None:
        """Read STATUS again soon (the checklist): after a command that may have changed the state."""
        self._status_due = True

    def _maybe_poll_status(self, now: float) -> None:
        m = self.machine
        if not m.connected or self._status_polling or self.runner is not None or self.busy_ops:
            return
        s = m.latest
        if (s is not None and s.moving) or (not self._status_due and now - self._last_status_poll < STATUS_POLL_S):
            return
        self._status_polling, self._status_due, self._last_status_poll = True, False, now

        def done(_status):
            self._status_polling = False
            self.refresh_readiness()

        def failed(_exc):
            self._status_polling = False

        self.run_async(m.status, done, failed, busy=False)

    def _restore_sections(self) -> None:
        """Folded or open, as the operator left each section."""
        states = self._gui_prefs().get("sections")
        states = states if isinstance(states, dict) else {}
        for section in self.findChildren(Section):
            if section.key and isinstance(states.get(section.key), bool):
                section.set_expanded(states[section.key], emit=False)
            section.toggled.connect(lambda on, key=section.key: self._save_section(key, on))

    def _save_section(self, key: str, on: bool) -> None:
        if key:
            states = self._gui_prefs().get("sections")
            states = dict(states) if isinstance(states, dict) else {}
            states[key] = on
            self._save_gui(sections=states)

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

    def status(self, text: str, level: str = "info") -> None:
        """A status-bar message for 15 s; `level` (info, success, warning, danger) colours it."""
        color = getattr(theme(), STATUS_COLORS.get(level, "text"))
        self.statusBar().setStyleSheet(f"QStatusBar {{ color: {color}; }}")
        self.statusBar().showMessage(text, 15000)

    def show_warnings(self, warnings: list[str]) -> None:
        """The banner: one line per warning; red when any of them is critical."""
        self._warnings = list(warnings)
        self._dismissed &= set(self._warnings)          # a warning that went away may come back later
        shown = [w for w in self._warnings if w not in self._dismissed or is_critical(w)]
        lines = [f"⚠&nbsp;<b>{html.escape(w)}</b>" if is_critical(w) else f"⚠&nbsp;{html.escape(w)}"
                 for w in shown]
        self.banner_text.setText("<br>".join(lines))
        critical = any(is_critical(w) for w in shown)
        themed(self.banner, lambda c=critical: banner_style(c))
        self.banner_close.setVisible(not all(is_critical(w) for w in shown))
        self.banner.setVisible(bool(shown))

    def dismiss_warnings(self) -> None:
        """The banner's ×: hide the warnings showing now (not the critical ones)."""
        self._dismissed |= {w for w in self._warnings if not is_critical(w)}
        self.show_warnings(self._warnings)

    def _show_connection(self, text: str, color: str) -> None:
        """The top bar: a coloured dot (grey off, amber connecting, green Mega, purple simulator) and the text."""
        self._conn = (text, color)
        self.conn_label.setText(dot(getattr(theme(), color), text))
        set_role(self.connect_btn, None if self.machine.connected else "primary")

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
                self.results_tab.refresh_status()
                self._update_status_bar()
                self._maybe_poll_status(now)
                self.refresh_readiness()
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
            if msg.ok and msg.command in STATUS_AFTER:
                self.poll_status()
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
            self.status(text, "danger" if reason in SAFETY_STOPS else "info")
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
            self.status(f"Overload: {u.format_force(ev.number('load'), 1)}. Back the load off.", "danger")
        elif ev.name == "READY":
            if self._ready_seen and self.machine.connected:
                # The Mega reset while connected (power glitch, reset button): it forgot the
                # stream/watchdog settings and its position.
                self.console.note("The Mega reset.")
                self._add_warning("The Mega reset: set the position reference again (Machine tab).")
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
        self._show_connection("Connecting...", "warning")
        self.refresh_readiness()

        def work():
            info = self.machine.connect(None if sim else port, sim=sim)
            if self.machine.sim is not None and not self.machine.status().referenced:
                self.machine.zero()   # the simulator starts unreferenced; its zero is as good as any
                self.machine.warnings = [w for w in self.machine.warnings if "referenced" not in w]
                self.machine.status()  # the checklist reads the referenced flag from the last STATUS
            return info

        self.run_async(work, self._on_connected, self._on_connect_failed, busy=False)

    def _on_connected(self, info) -> None:
        self._connecting = False
        self._ready_seen = True
        self.connect_btn.setText("Disconnect")
        where = "simulator" if info.simulated else self.machine.port
        self._show_connection(f"{where} · {info.name} {info.version}", "purple" if info.simulated else "success")
        self._saved_position = self.machine.saved_position() is not None
        for panel in self.panels:
            panel.on_connected()
        self.show_warnings(self.machine.warnings)
        self.check_storage()
        self.console.note(f"connected to {where}: {info.name} {info.version} (protocol {info.protocol})")
        self.status(f"Connected to {where}", "success")
        self.refresh_readiness()
        if not info.simulated and self._saved_position and any("referenced" in w for w in self.machine.warnings):
            self.jog_panel.restore()   # asks first

    def _on_connect_failed(self, exc: BaseException) -> None:
        self._connecting = False
        self._show_connection("Not connected", "muted")
        self.refresh_readiness()
        text = str(exc) or type(exc).__name__
        if isinstance(exc, FirmwareMismatch):
            if exc.banner:
                text += "\n\nThe board printed:\n" + "\n".join(exc.banner[:6])
            text += "\n\nSystem > Firmware can upload the right firmware."
            self.reveal(self.firmware_panel)
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
        self._show_connection("Not connected", "muted")
        self._remove_warnings("referenced")
        self.firmware_panel.update_info()
        self.status("Disconnected")
        self.refresh_readiness()

    # ------------------------------------------------------------------ STOP --
    def stop_all(self) -> None:
        if self.runner is not None:
            self.runner.request_stop()
        if self.machine.connected:
            try:
                self.machine.stop()
            except LinkClosed:
                pass
            self.status("STOP sent", "warning")

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
        self.results_view.mark_stale(result.folder)
        self.dic_view.mark_stale(result.folder)
        s = result.summary
        if s is not None and s.n_samples:
            self.plots.mark_peak(s.extension_at_peak_mm, s.peak_load_n)
        if any("sign is reversed" in w for w in result.warnings):
            self._add_warning(TENSION_WARNING)
        self.status(f"Test finished ({result.stop_reason}). Files: {result.folder}",
                    "danger" if result.aborted or result.stop_reason in SAFETY_STOPS else "success")
        if result.folder is not None and (result.folder / "camera").is_dir() and self.camera is not None:
            self.last_camera_folder = result.folder / "camera"
            self._pending_sync = result.folder
            if self.camera.recording is None:          # the camera already finished writing
                self._sync_test(result.folder)
        else:
            self.postprocess.resume()
            if result.folder is not None and self.config.storage.upload_zips:
                self.make_upload_zip(result.folder)     # no camera jobs: zip it now (small)
        self.check_storage()

    def make_upload_zip(self, folder: Path) -> None:
        """Outputs/upload/<date>/<test>.zip, in the background."""
        from vsbc.share import build_upload_zip

        output_dir = self.config.output_dir
        self.run_async(lambda: build_upload_zip(folder, output_dir),
                       lambda path: self.console.note(f"Upload zip: {path}"),
                       lambda exc: self._add_warning(f"Could not make the upload zip: {exc}"), busy=False)

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

    def queue_postprocess(self, folder: Path, options=None) -> None:
        """Process a folder: a test folder, its camera/ folder, or a recording made from the Camera tab.

        `options` (the DIC tab's choices) replaces the Camera panel's "After the test" choices."""
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
        if options is None:
            options = self.camera_panel.post_options() if self.camera is not None else None
        if options is None:
            return
        delay = self.camera_panel.load_delay.value() if self.camera is not None else self.config.camera.load_delay_ms
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
        self.disk_status.setText(dot(level_color(status.level), f"Disk: {human_size(status.free)} free"))
        self.disk_status.setToolTip(status.text())
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
        self.results_view.apply_units(units)
        self.dic_view.apply_units(units)
        for panel in self.panels:
            panel.apply_units(units)
        self.status(f"Units: {units.force}, {units.length}")

    def set_theme(self, name: str, remember: bool = True) -> None:
        """Light or dark (Display tab), now and (remember) next time."""
        set_app_theme(QtWidgets.QApplication.instance(), name)
        if remember:
            self._save_gui(theme=theme().name)
        self.plots.restyle()
        self.show_warnings(self._warnings)
        self._show_connection(*self._conn)
        self._shown_state = None
        self.check_storage()
        self._update_status_bar()
        for panel in self.panels:
            panel.restyle()

    # ---------------------------------------------------------- remembering --
    def _gui_prefs(self) -> dict:
        data = load_preferences(self.config.preferences_file).get("gui")
        return data if isinstance(data, dict) else {}

    def _save_gui(self, **updates) -> None:
        data = self._gui_prefs()
        data.update(updates)
        save_preferences(self.config.preferences_file, {"gui": data})

    @staticmethod
    def _tab_name(tabs, index: int) -> str:
        return tabs.tabText(index).replace(" ⚠", "") if 0 <= index < tabs.count() else ""

    @staticmethod
    def _find_tab(tabs, name) -> int:
        for i in range(tabs.count()):
            if tabs.tabText(i).replace(" ⚠", "") == name:
                return i
        return -1

    def _restore_layout(self) -> None:
        """Last session's splitter and tabs (the window size is restored by run_gui)."""
        g = self._gui_prefs()
        sizes = g.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(s, int) and s > 0 for s in sizes):
            self.splitter.setSizes(sizes)
        index = self._find_tab(self.tabs, g.get("tab"))
        if index >= 0:
            self.tabs.setCurrentIndex(index)
        index = self._find_tab(self.left_tabs, g.get("plot_tab"))
        if index >= 0 and index != self.results_index:      # Results reads files: not at start-up
            self.left_tabs.setCurrentIndex(index)

    def save_layout(self) -> None:
        updates = {"splitter": self.splitter.sizes(), "tab": self._tab_name(self.tabs, self.tabs.currentIndex()),
                   "plot_tab": self._tab_name(self.left_tabs, self.left_tabs.currentIndex())}
        if not self.isFullScreen():
            updates["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
            updates["maximized"] = self.isMaximized()
        self._save_gui(**updates)

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
        if state != self._shown_state:
            self._shown_state = state
            themed(self.state_label, lambda st=state: pill_style(state_color(st)))
            self.state_card.set_color({"OFFLINE": "muted", "IDLE": "accent", "MOVE": "warning", "RUN": "success",
                                       "TEST": "success"}.get(state, "purple"))

    def _update_status_bar(self) -> None:
        """Camera state and post-processing, on the slow tick."""
        t = theme()
        cam = self.camera
        if cam is not None:
            text = dot(getattr(t, CAMERA_STATE_COLORS.get(cam.state, "muted")), f"Camera: {cam.state}")
            if text != self.camera_status.text():
                self.camera_status.setText(text)
        pp = self.postprocess
        if pp.busy or getattr(pp, "queue", None):
            summary = pp.summary()
            short = summary if len(summary) <= 60 else summary[:57] + "..."
            self.post_status.setText(f"<a href='dic' style='color:{t.text}; text-decoration:none'>"
                                     f"{dot(t.accent, f'Processing: {short}')}</a>")
            self.post_status.setToolTip(summary)
            self.post_status.show()
        else:
            self.post_status.hide()

    def _update_enabled(self) -> None:
        connected = self.machine.connected
        s = self.machine.latest
        busy = self.busy_ops > 0 or self.runner is not None or (connected and s is not None and s.moving)
        if not connected:
            self.disabled_reason = "Connect to the machine first (Connect, top left)."
        elif self.runner is not None:
            self.disabled_reason = "A test is running."
        elif s is not None and s.moving:
            self.disabled_reason = "The crosshead is moving: wait for it, or press STOP."
        elif busy:
            self.disabled_reason = "Waiting for the machine to finish the last command."
        else:
            self.disabled_reason = ""
        for panel in self.panels:
            panel.update_enabled(connected, busy)
        cam = self.camera
        available = self.runner is None and not (cam is not None and (cam.recording is not None
                                                                       or cam.state in ("recording", "finishing")))
        if available != self._results_available:
            # Reading frames would compete with the camera for the disk.
            self._results_available = available
            self.results_tab.set_available(available)
            self.left_tabs.setTabEnabled(self.results_index, available)
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
                        "Results > DIC analysis."):
            event.ignore()
            return
        try:
            self.save_layout()
            self.test_panel.save_settings()
        except Exception:  # noqa: BLE001 - never block quitting
            log.exception("could not save the window layout")
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
    prefs = load_preferences(config.preferences_file).get("gui")
    prefs = prefs if isinstance(prefs, dict) else {}
    font_px = apply_style(app, config.gui.font_pt, config.gui.scale, theme_name=str(prefs.get("theme", "light")))
    width, height = screen_size()
    log.info("screen %d × %d px: text %.1f px", width, height, font_px)
    win = MainWindow(config, port=port, sim=sim, auto_connect=bool(port or sim))
    geometry = prefs.get("geometry")
    if fullscreen:
        win.showFullScreen()
    elif isinstance(geometry, str) and win.restoreGeometry(QtCore.QByteArray.fromBase64(geometry.encode("ascii"))):
        if prefs.get("maximized"):
            win.showMaximized()
        else:
            win.show()
    elif width < 1300 or height < 860:          # 1280 × 800 and the title bar would not fit
        win.showMaximized()
    else:
        win.resize(1280, 800)
        win.show()
    return exec_app(app)
