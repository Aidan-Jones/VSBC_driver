"""High-level, thread-safe API for the tensile tester.

`Machine` wraps a `SerialLink` (real Mega or `SimulatedMega`): it does the
connect handshake, keeps the latest sample, forwards messages to listeners,
pings the firmware watchdog and offers one method per firmware command.
Units: mm, N, mm/s (use mm_min_to_mm_s for test rates in mm/min).
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from vsbc import FIRMWARE_VERSION, PROTOCOL_VERSION
from vsbc.config import Config, load_config
from vsbc.link import DeviceError, LinkClosed, SerialLink, SessionLog, find_port, open_serial
from vsbc.protocol import Event, Info, Message, Reply, Sample, settings_from_reply, to_float

log = logging.getLogger(__name__)


def mm_min_to_mm_s(rate_mm_min: float) -> float:
    return rate_mm_min / 60.0


def mm_s_to_mm_min(rate_mm_s: float) -> float:
    return rate_mm_s * 60.0


class FirmwareMismatch(RuntimeError):
    """The board did not answer like vsbc_firmware (wrong sketch, or old protocol)."""

    def __init__(self, message: str, banner: list[str] | None = None):
        self.banner = banner or []
        super().__init__(message)


@dataclass(frozen=True)
class FirmwareInfo:
    name: str
    version: str
    protocol: int
    simulated: bool = False


@dataclass(frozen=True)
class Status:
    """`STATUS` reply."""

    state: str            # IDLE, MOVE or RUN
    pos_mm: float
    load_n: float
    raw: int
    rate_mm_s: float
    motors: str
    referenced: bool
    loadcell_ok: bool
    calibrated: bool
    sample_rate_hz: float
    streaming: bool
    dropped_lines: int

    @classmethod
    def from_reply(cls, r: Reply) -> "Status":
        def flag(key):
            return r.get(key) == "1"

        return cls(
            state=r.get("state", "IDLE"),
            pos_mm=r.number("pos"),
            load_n=r.number("load"),
            raw=int(r.number("raw", 0)),
            rate_mm_s=r.number("rate"),
            motors=r.get("motors", "AB"),
            referenced=flag("ref"),
            loadcell_ok=flag("lc"),
            calibrated=flag("cal"),
            sample_rate_hz=r.number("sps", 0.0),
            streaming=flag("stream"),
            dropped_lines=int(r.number("drops", 0)),
        )


class Machine:
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self.link: SerialLink | None = None
        self.sim = None                       # SimulatedMega when simulating
        self.info: FirmwareInfo | None = None
        self.port: str | None = None
        self.settings: dict[str, float] = {}
        self.latest: Sample | None = None
        self.sample_rate_hz = 10.0            # updated from STATUS
        self.warnings: list[str] = []         # connect-time warnings for the UI
        self._listeners: list[Callable[[Message], None]] = []
        self._raw_listeners: list[Callable[[str, bool], None]] = []
        self._move_end: queue.Queue[Event] = queue.Queue()
        self._stop_ping = threading.Event()
        self._ping_thread: threading.Thread | None = None

    # ------------------------------------------------------------ listeners --
    def add_listener(self, fn: Callable[[Message], None]) -> None:
        """fn(msg) for every Sample/Reply/Event/Info, called on the serial reader thread."""
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def add_raw_listener(self, fn: Callable[[str, bool], None]) -> None:
        """fn(text, sent) for every line sent or received."""
        self._raw_listeners.append(fn)
        if self.link:
            self.link.add_raw_listener(fn)

    def remove_raw_listener(self, fn) -> None:
        if fn in self._raw_listeners:
            self._raw_listeners.remove(fn)
        if self.link:
            self.link.remove_raw_listener(fn)

    # ----------------------------------------------------------- connection --
    @property
    def connected(self) -> bool:
        return self.link is not None and not self.link.closed

    def connect(self, port: str | None = None, sim=False, *, stream: bool = True, watchdog: bool = True) -> FirmwareInfo:
        """Open the port, wait for the firmware and configure it.

        `sim` may be True (default simulator) or a SimulatedMega instance.
        With watchdog=False (terminal use) the firmware watchdog is turned off.
        """
        if self.connected:
            self.disconnect()
        self.warnings = []

        if sim:
            from vsbc.sim import SimulatedMega

            transport = sim if isinstance(sim, SimulatedMega) else SimulatedMega(boot_delay_s=0.3, lead_error=0.995)
            self.sim = transport
            port_name = "SIM"
        else:
            self.sim = None
            port_name = port or self.config.serial.port or find_port()
            transport = open_serial(port_name, self.config.serial.baud)

        started = datetime.now()
        session_log = SessionLog(
            self.config.logs_dir / f"{started:%Y-%m-%d_%H-%M-%S}_serial.log",
            header=[
                "VSBC serial session",
                f"Started: {started:%Y-%m-%d %H:%M:%S}",
                f"Port:    {port_name} @ {self.config.serial.baud} baud",
            ],
            data_lines=self.config.serial.log_data_lines,
        )
        link = SerialLink(transport, session_log)
        ready = threading.Event()
        ready_fields: dict[str, str] = {}
        banner: list[str] = []

        def early(msg: Message) -> None:
            if isinstance(msg, Event) and msg.name == "READY":
                ready_fields.update(msg.fields)
                ready.set()
            elif isinstance(msg, Info) and msg.text:
                banner.append(msg.text)

        link.add_listener(early)
        link.add_listener(self._on_message)
        for fn in self._raw_listeners:
            link.add_raw_listener(fn)
        link.start()

        try:
            if ready.wait(self.config.serial.ready_timeout_s):
                fields = ready_fields
            else:
                # No banner (board did not reset, or it is not our firmware): ask.
                try:
                    fields = link.command("ID", timeout=1.5).fields
                except (TimeoutError, DeviceError) as e:
                    raise FirmwareMismatch(
                        f"No answer from vsbc_firmware on {port_name} ({e}). "
                        "Upload firmware/vsbc_firmware to the Mega.",
                        banner,
                    ) from None
            proto = int(to_float(fields.get("proto"), -1))
            if proto != PROTOCOL_VERSION:
                raise FirmwareMismatch(
                    f"Firmware on {port_name} speaks protocol {proto}, this app needs {PROTOCOL_VERSION}. "
                    "Upload firmware/vsbc_firmware.",
                    banner,
                )
            self.info = FirmwareInfo(
                fields.get("name", "?"), fields.get("ver", "?"), proto, simulated=self.sim is not None
            )
            if self.info.version != FIRMWARE_VERSION:
                self.warnings.append(
                    f"Firmware version {self.info.version} differs from the expected {FIRMWARE_VERSION}."
                )
            self.link = link
            self.port = port_name
            self.set("watchdog_ms", self.config.watchdog_ms if watchdog else 0)
            self.stream(stream)
            if stream:
                self._wait_for_load_cell(1.5)
            self.refresh_settings()
            status = self.status()
            if not status.loadcell_ok:
                self.warnings.append("Load cell not responding (check the HX711 wiring).")
            elif not status.calibrated:
                self.warnings.append("Load cell not calibrated.")
            if not status.referenced:
                self.warnings.append("Position not referenced: set zero at the reference position.")
        except BaseException:
            link.close()
            self.link = None
            if self.sim is not None:
                self.sim.close()
                self.sim = None
            raise
        finally:
            link.remove_listener(early)

        if watchdog:
            self._stop_ping.clear()
            self._ping_thread = threading.Thread(target=self._ping_loop, name="vsbc-ping", daemon=True)
            self._ping_thread.start()
        log.info("connected to %s: %s %s (protocol %d)", port_name, self.info.name, self.info.version, proto)
        return self.info

    def disconnect(self) -> None:
        """Stop any move, remember the position, close the port."""
        self._stop_ping.set()
        link = self.link
        if link is None:
            return
        if not link.closed:
            try:
                self.stop()
                status = self.status()
                if status.state == "IDLE":
                    self._save_position(status)
            except (LinkClosed, TimeoutError, DeviceError, OSError) as e:
                log.warning("disconnect: %s", e)
        link.close()
        self.link = None
        if self.sim is not None:
            self.sim.close()
            self.sim = None
        log.info("disconnected")

    def _wait_for_load_cell(self, timeout: float) -> bool:
        """After a reset the first load-cell conversion can take a moment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.latest
            if s is not None and s.raw != 0:
                return True
            time.sleep(0.02)
        return False

    def _ping_loop(self) -> None:
        interval = self.config.ping_interval_s
        while not self._stop_ping.wait(interval):
            link = self.link
            if link is None or link.closed:
                return
            if time.monotonic() - link.last_tx < interval:
                continue
            try:
                link.command("PING", timeout=1.0)
            except (TimeoutError, DeviceError, LinkClosed) as e:
                log.debug("ping: %s", e)

    def _on_message(self, msg: Message) -> None:
        if isinstance(msg, Sample):
            self.latest = msg
        elif isinstance(msg, Event) and msg.name == "MOVE_END":
            self._move_end.put(msg)
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:  # noqa: BLE001
                log.exception("machine listener failed")

    # ------------------------------------------------------------- commands --
    def _link(self) -> SerialLink:
        if not self.connected:
            raise LinkClosed("not connected")
        return self.link

    def command(self, name: str, *args, timeout: float = 2.0) -> Reply:
        return self._link().command(name, *args, timeout=timeout)

    def send_raw(self, text: str) -> None:
        """Console: send a line as typed; the reply is not waited for."""
        self._link().write_line(text.strip())

    def stop(self) -> None:
        """Stop now. Never waits behind another command."""
        link = self.link
        if link is not None and not link.closed:
            link.write_line("STOP")

    def status(self) -> Status:
        status = Status.from_reply(self.command("STATUS"))
        if status.sample_rate_hz > 0:
            self.sample_rate_hz = status.sample_rate_hz
        return status

    def refresh_settings(self) -> dict[str, float]:
        self.settings = settings_from_reply(self.command("GET"))
        return self.settings

    def set(self, key: str, value) -> Reply:
        reply = self.command("SET", key, value)
        for k, v in reply.fields.items():
            self.settings[k] = to_float(v)
        return reply

    def save(self) -> Reply:
        return self.command("SAVE")

    def defaults(self) -> Reply:
        reply = self.command("DEFAULTS")
        self.refresh_settings()
        return reply

    def stream(self, on: bool) -> Reply:
        return self.command("STREAM", "ON" if on else "OFF")

    def set_rate(self, mm_s: float) -> Reply:
        return self.command("RATE", float(mm_s))

    def max_rate(self) -> float:
        """mm/s at the motor speed limit (800 steps/s)."""
        spm = self.settings.get("steps_per_mm", 80.0)
        return 800.0 / spm if spm > 0 else 10.0

    def _drain_move_end(self) -> None:
        while True:
            try:
                self._move_end.get_nowait()
            except queue.Empty:
                return

    def move_by(self, mm: float, rate_mm_s: float | None = None) -> Reply:
        if rate_mm_s is not None:
            self.set_rate(rate_mm_s)
        self._drain_move_end()
        return self.command("MOVE", float(mm))

    def move_to(self, mm: float, rate_mm_s: float | None = None) -> Reply:
        if rate_mm_s is not None:
            self.set_rate(rate_mm_s)
        self._drain_move_end()
        return self.command("GOTO", float(mm))

    def home(self, rate_mm_s: float | None = None) -> Reply:
        if rate_mm_s is not None:
            self.set_rate(rate_mm_s)
        self._drain_move_end()
        return self.command("HOME")

    def run(self, mm: float) -> Reply:
        """Test move at the current rate, with break/load stops armed."""
        self._drain_move_end()
        return self.command("RUN", float(mm))

    def wait_move_end(self, timeout: float | None = None) -> Event:
        try:
            return self._move_end.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("move did not finish in time") from None

    def move_and_wait(self, mm: float, rate_mm_s: float | None = None, absolute: bool = False) -> Event:
        reply = self.move_to(mm, rate_mm_s) if absolute else self.move_by(mm, rate_mm_s)
        return self.wait_move_end(timeout=reply.number("expected", 0.0) + 5.0)

    def zero(self, position_mm: float = 0.0) -> Reply:
        return self.command("ZERO", float(position_mm))

    def _average_timeout(self) -> float:
        return 16 / max(self.sample_rate_hz, 5.0) * 2 + 3.0

    def tare(self) -> Reply:
        self.status()   # refresh the sample rate for the timeout
        return self.command("TARE", timeout=self._average_timeout())

    def calibrate_span(self, known_n: float) -> Reply:
        self.status()
        return self.command("CAL", float(known_n), timeout=self._average_timeout())

    def select_motors(self, which: str) -> Reply:
        return self.command("MOTORS", which.upper())

    # -------------------------------------------------- position persistence --
    def _save_position(self, status: Status) -> None:
        if not status.referenced or math.isnan(status.pos_mm) or self.sim is not None:
            return
        path = self.config.state_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "pos_mm": status.pos_mm,
                "saved": datetime.now().isoformat(timespec="seconds"),
                "port": self.port,
            }, indent=2))
        except OSError as e:
            log.warning("could not save the position: %s", e)

    def saved_position(self) -> dict | None:
        """Position saved at the last clean disconnect, if any."""
        try:
            data = json.loads(self.config.state_file.read_text())
            float(data["pos_mm"])
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None
