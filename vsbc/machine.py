"""High-level, thread-safe API for the tensile tester.

`Machine` wraps a `SerialLink` (real Mega or `SimulatedMega`): it does the
connect handshake, keeps the latest sample, forwards messages to listeners,
pings the firmware watchdog and offers one method per firmware command. By
default the Mega reads the load-cell amplifier itself (config.loadcell.source
= "mega"); with source = "pi" it reads the amplifier on the Pi's GPIO pins
and feeds every reading to the Mega.
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

# The firmware's TARE / CAL average this long, and at least 16 samples (config.h AVERAGE_MS).
AVERAGE_S = 2.0


def mm_min_to_mm_s(rate_mm_min: float) -> float:
    return rate_mm_min / 60.0


def mm_s_to_mm_min(rate_mm_s: float) -> float:
    return rate_mm_s * 60.0


def loadcell_rate_problem(measured_hz: float, config: Config) -> str | None:
    """A warning when the measured load-cell rate does not match [loadcell] rate_hz."""
    expected = config.loadcell.rate_hz
    if measured_hz <= 0 or 0.7 * expected <= measured_hz <= 1.3 * expected:
        return None
    text = (f"The load cell sends {measured_hz:.0f} readings/s, but rate_hz = {expected:g} under [loadcell] "
            f"in {config.source or 'config.toml'}.")
    if measured_hz < 20 <= expected:
        return text + (" The HX711 is still at its shipped 10 readings/s: cut its RATE jumper for 80 "
                       "(docs/hardware.md), or set rate_hz = 10.")
    return text + f" Set rate_hz = {measured_hz:.0f} if that is how the HX711 is set up."


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
    loadcell_from_host: bool = False
    loadcell_discarded: int | None = None   # HX711 readings the Mega threw away; None before firmware 2.1.1

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
            loadcell_from_host=flag("lchost"),
            loadcell_discarded=int(r.number("lcbad")) if "lcbad" in r.fields else None,
        )


class Machine:
    def __init__(self, config: Config | None = None, loadcell_reader=None):
        """`loadcell_reader` replaces the HX711 on the Pi's GPIO pins (tests): any
        object with read() -> int | None and close()."""
        self.config = config or load_config()
        self.loadcell_source = "mega"         # "pi" while this host feeds the readings
        self._loadcell_reader = loadcell_reader
        self._feeder = None
        self.link: SerialLink | None = None
        self.sim = None                       # SimulatedMega when simulating
        self.info: FirmwareInfo | None = None
        self.port: str | None = None
        self.settings: dict[str, float] = {}
        self.latest: Sample | None = None
        self.sample_rate_hz = self.config.loadcell.rate_hz   # updated from STATUS
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

    def connect(self, port: str | None = None, sim=False, *, stream: bool = True, watchdog: bool = True,
                loadcell: bool = True) -> FirmwareInfo:
        """Open the port, wait for the firmware and configure it.

        `sim` may be True (default simulator) or a SimulatedMega instance.
        With watchdog=False (terminal use) the firmware watchdog is turned off.
        With loadcell=False the Pi's load-cell pins are left alone.
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
            if loadcell:
                self._start_host_load_cell()
                self._check_host_load_cell(1.5)
                self._apply_pending_load_calibration()
            self.stream(stream)
            if stream:
                self._wait_for_load_cell(1.5)
            self.refresh_settings()
            status = self.status()
            if not status.loadcell_ok:
                self.warnings.append(self.loadcell_problem(status))
            elif not status.calibrated:
                self.warnings.append("Load cell not calibrated.")
            rate_problem = self.rate_problem(status.sample_rate_hz) if status.loadcell_ok else None
            if rate_problem:
                self.warnings.append(rate_problem)
            if not status.referenced:
                self.warnings.append("Position not referenced: set zero at the reference position.")
        except BaseException:
            self._stop_host_load_cell()
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
        log.info("connected to %s: %s %s (protocol %d), load cell read by the %s; config: %s", port_name,
                 self.info.name, self.info.version, proto, "Pi" if self.loadcell_source == "pi" else "Mega",
                 self.config_description())
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
        self._stop_host_load_cell()
        link.close()
        self.link = None
        if self.sim is not None:
            self.sim.close()
            self.sim = None
        log.info("disconnected")

    def _start_host_load_cell(self) -> None:
        """Read the HX711 on the Pi's GPIO pins and feed the Mega (`LC <raw>`).

        Falls back to the HX711 wired to the Mega, with a warning, when the
        Pi's GPIO is not available (for example on a PC).
        """
        from vsbc.loadcell_pi import LoadCellFeeder, LoadCellUnavailable, PiHX711

        self.loadcell_source = "mega"
        reader = self._loadcell_reader
        lc = self.config.loadcell
        if reader is None and lc.source == "pi" and self.sim is None:
            try:
                reader = PiHX711(lc.dout_pin, lc.sck_pin, lc.gain)
            except LoadCellUnavailable as e:
                self.warnings.append(
                    f"Cannot read the load cell on the Pi's GPIO pins ({e}). Using the HX711 wired to the Mega instead; "
                    f'set source = "mega" under [loadcell] in {self._config_name()} to make that the choice.'
                )
        if reader is None:
            # A reset clears lc_host, but the Mega does not always reset when the port
            # opens: a 1 left over from an earlier session would hide its own HX711.
            self.set("lc_host", 0)
            return
        link = self.link
        try:
            self.set("lc_host", 1)
        except BaseException:
            reader.close()
            raise
        self._feeder = LoadCellFeeder(reader, lambda raw: link.write_line(f"LC {raw}", quiet=True))
        self._feeder.start()
        self.loadcell_source = "pi"

    def _check_host_load_cell(self, timeout: float) -> None:
        """Fall back to the HX711 wired to the Mega when the Pi's reader delivers nothing."""
        feeder = self._feeder
        if feeder is None:
            return
        deadline = time.monotonic() + timeout
        while feeder.samples == 0 and feeder.error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if feeder.samples > 0:
            return
        self._stop_host_load_cell()
        self.set("lc_host", 0)
        self.loadcell_source = "mega"
        lc = self.config.loadcell
        self.warnings.append(
            f"No readings from the HX711 on the Pi's GPIO{lc.dout_pin} / GPIO{lc.sck_pin} pins: using the HX711 "
            f'wired to the Mega instead. Set source = "mega" under [loadcell] in {self._config_name()} '
            "if that is where it is wired."
        )
        log.warning("no readings from the HX711 on the Pi: falling back to the Mega's HX711")

    def _config_name(self) -> str:
        return str(self.config.source) if self.config.source else "config.toml"

    def config_description(self) -> str:
        """Which config file was read and the load-cell source it selects."""
        where = self.config.source or "no config file, built-in defaults"
        return f'{where}, source = "{self.config.loadcell.source}"'

    def loadcell_problem(self, status: Status) -> str:
        """Why the load cell is not responding, and what to check."""
        if self.loadcell_source == "pi":
            lc = self.config.loadcell
            why = (f"no readings from the HX711 on the Pi's GPIO{lc.dout_pin} (DAT) / GPIO{lc.sck_pin} (CLK): "
                   "check its wiring (VDD to 3.3V, VCC to 5V, GND)")
        elif status.loadcell_from_host:
            why = "the Mega is waiting for readings from the Pi (lc_host = 1)"
        elif status.loadcell_discarded is None:   # firmware before 2.1.1 does not say more
            why = "the Mega gets no readings from the HX711 on D26 (DAT) / D27 (CLK): check its wiring (VCC and VDD to 5V, GND)"
        elif status.loadcell_discarded == 0:
            why = ("the Mega sees no conversions, DAT (D26) stays high: check the HX711's VCC and VDD (5V), "
                   "GND, and DAT on D26")
        else:
            why = "DAT (D26) stays low after the clock pulses: check CLK on D27 and VDD (5V)"
        return f"Load cell not responding: {why}. (Config: {self.config_description()}.)"

    def rate_problem(self, measured_hz: float) -> str | None:
        return loadcell_rate_problem(measured_hz, self.config)

    def reconfigure_after_reset(self) -> dict[str, float]:
        """The Mega reset while connected: it forgot the watchdog, the stream and the load-cell source."""
        self.set("watchdog_ms", self.config.watchdog_ms)
        self.set("lc_host", 1 if self.loadcell_source == "pi" else 0)
        self.stream(True)
        return self.refresh_settings()

    def _apply_pending_load_calibration(self) -> None:
        """Store a calibration made without the Mega (`vsbc calibrate load --no-mega`).

        It was measured with the HX711 on the Pi, so it is only applied while
        the Pi is the load-cell source; otherwise the file keeps waiting.
        """
        from vsbc.calibration import load_pending_load

        path = self.config.pending_load_file
        if self.loadcell_source != "pi" or not path.is_file():
            return
        pending = load_pending_load(path)
        if pending is None:
            self.warnings.append(f"Could not read the waiting load-cell calibration in {path}; it was not applied.")
            return
        try:
            self.set("load_scale", pending["scale"])
            self.set("load_offset", pending["offset"])
            self.save()
        except DeviceError as e:
            self.warnings.append(f"The load-cell calibration made without the Mega was refused ({e}); it is still in {path}.")
            return
        path.unlink(missing_ok=True)
        notice = (f"Stored the load-cell calibration made without the Mega on {pending['created']} "
                  f"({pending['scale']:.4f} counts/N).")
        if self.refresh_settings().get("max_load", 0.0) <= 0:
            notice += " The overload cutoff (max_load) is off: set it in the Calibrate tab."
        self.warnings.append(notice)
        log.info(notice)

    def _stop_host_load_cell(self) -> None:
        feeder, self._feeder = self._feeder, None
        if feeder is not None:
            feeder.stop()

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
        """mm/s at the motor speed limit (6400 steps/s)."""
        spm = self.settings.get("steps_per_mm", 640.0)
        return 6400.0 / spm if spm > 0 else 10.0

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
        return max(AVERAGE_S, 16 / max(self.sample_rate_hz, 5.0)) * 2 + 3.0

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
