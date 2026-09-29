"""Serial link to the Mega: port discovery, a reader thread, command/reply
matching and a timestamped session log.

A *transport* is anything with ``write(bytes)``, ``readline() -> bytes`` (returns
b"" on timeout) and ``close()``: a pyserial ``Serial`` or ``sim.SimulatedMega``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from vsbc.protocol import Event, Message, ProtocolError, Reply, format_command, parse_line

log = logging.getLogger(__name__)

# USB vendor IDs: Arduino, Arduino.org, WCH CH340 (common on Mega clones), FTDI.
ARDUINO_VIDS = {0x2341, 0x2A03, 0x1A86, 0x0403}


class DeviceError(RuntimeError):
    """The firmware answered ERR."""

    def __init__(self, reply: Reply):
        self.reply = reply
        self.code = reply.code
        super().__init__(f"{reply.command}: {reply.code} {reply.message}".strip())


class LinkClosed(RuntimeError):
    """The link was closed or the serial port failed."""


class PortNotFound(RuntimeError):
    pass


def list_ports() -> list[tuple[str, str]]:
    """(device, description) for every serial port, Arduino-looking ones first."""
    from serial.tools import list_ports as lp

    ports = sorted(lp.comports(), key=lambda p: (p.vid not in ARDUINO_VIDS, p.device))
    return [(p.device, p.description or "") for p in ports]


def find_port() -> str:
    """The Mega's port if it can be picked unambiguously."""
    from serial.tools import list_ports as lp

    ports = list(lp.comports())
    arduino = [p for p in ports if p.vid in ARDUINO_VIDS]
    if len(arduino) == 1:
        return arduino[0].device
    if len(ports) == 1:
        return ports[0].device
    if not ports:
        raise PortNotFound("No serial ports found. Is the Mega plugged in?")
    names = ", ".join(f"{p.device} ({p.description})" for p in ports)
    raise PortNotFound(f"Could not pick the Mega's port automatically. Available: {names}. Use --port.")


def open_serial(port: str, baud: int = 115200):
    import serial

    try:
        return serial.Serial(port, baud, timeout=0.1, write_timeout=1.0)
    except serial.SerialException as e:
        raise LinkClosed(f"Could not open {port}: {e} (close the Arduino Serial Monitor if it is open)") from e


class SessionLog:
    """Timestamped log of every line sent and received."""

    def __init__(self, path: Path | None, header: list[str] = (), data_lines: bool = False):
        self.path = path
        self.data_lines = data_lines
        self._lock = threading.Lock()
        self._file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "w", encoding="utf-8")
            for line in header:
                self._file.write(f"# {line}\n")
            self._file.flush()

    def write(self, text: str, sent: bool = False) -> None:
        if self._file is None or (not self.data_lines and text.startswith("D ")):
            return
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with self._lock:
            if self._file:
                self._file.write(f"[{stamp}] {'> ' if sent else ''}{text}\n")
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


class SerialLink:
    """Reader thread plus one-command-at-a-time request/reply."""

    def __init__(self, transport, session_log: SessionLog | None = None):
        self._transport = transport
        self._log = session_log or SessionLog(None)
        self._listeners: list[Callable[[Message], None]] = []
        self._raw_listeners: list[Callable[[str, bool], None]] = []
        self._write_lock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._reply_cond = threading.Condition()
        self._pending: str | None = None
        self._reply: Reply | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._released = False
        self.closed = False
        self.error: str | None = None
        self.last_tx = 0.0
        self.last_rx = 0.0

    # ----------------------------------------------------------- listeners --
    def add_listener(self, fn: Callable[[Message], None]) -> None:
        """fn(msg) is called on the reader thread for every parsed line. Keep it fast."""
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def add_raw_listener(self, fn: Callable[[str, bool], None]) -> None:
        """fn(text, sent) for every line sent or received."""
        self._raw_listeners.append(fn)

    def remove_raw_listener(self, fn) -> None:
        if fn in self._raw_listeners:
            self._raw_listeners.remove(fn)

    # ----------------------------------------------------------- lifecycle --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_loop, name="vsbc-serial-reader", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        self.closed = True
        self._stop.set()
        with self._reply_cond:
            self._reply_cond.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001 - closing a dead port can raise anything
            pass
        self._log.close()

    # -------------------------------------------------------------- output --
    def write_line(self, text: str) -> None:
        """Send one line without waiting for a reply."""
        if self.closed:
            raise LinkClosed(self.error or "link closed")
        with self._write_lock:
            try:
                self._transport.write((text + "\n").encode("ascii", errors="replace"))
            except Exception as e:  # noqa: BLE001 - pyserial raises several types
                self._fail(f"write failed: {e}")
                raise LinkClosed(self.error) from e
            self.last_tx = time.monotonic()
        self._log.write(text, sent=True)
        for fn in list(self._raw_listeners):
            self._safe_call(fn, text, True)

    def command(self, name: str, *args, timeout: float = 2.0) -> Reply:
        """Send a command and wait for its OK/ERR. Raises DeviceError on ERR."""
        line = format_command(name, *args)
        cmd = line.split()[0]
        with self._cmd_lock:
            with self._reply_cond:
                self._pending = cmd
                self._reply = None
            try:
                self.write_line(line)
            except LinkClosed:
                self._pending = None
                raise
            deadline = time.monotonic() + timeout
            with self._reply_cond:
                while self._reply is None:
                    if self.closed:
                        self._pending = None
                        raise LinkClosed(self.error or "link closed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._pending = None
                        raise TimeoutError(f"no reply to {line!r} within {timeout:.1f} s")
                    self._reply_cond.wait(remaining)
                reply = self._reply
                self._pending = None
                self._reply = None
        if not reply.ok:
            raise DeviceError(reply)
        return reply

    # --------------------------------------------------------------- input --
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._transport.readline()
            except Exception as e:  # noqa: BLE001
                if not self._stop.is_set():
                    self._fail(f"read failed: {e}")
                break
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self.last_rx = time.monotonic()
            self._log.write(text)
            for fn in list(self._raw_listeners):
                self._safe_call(fn, text, False)
            try:
                msg = parse_line(text)
            except ProtocolError as e:
                log.warning("%s", e)
                continue
            if msg is None:
                continue
            if isinstance(msg, Reply):
                self._deliver_reply(msg)
            for fn in list(self._listeners):
                self._safe_call(fn, msg)

    def _deliver_reply(self, reply: Reply) -> None:
        with self._reply_cond:
            if self._pending is not None and reply.command == self._pending:
                self._reply = reply
                self._reply_cond.notify_all()
            else:
                log.debug("unsolicited reply: %s", reply)

    def _fail(self, message: str) -> None:
        if self.error is None:
            self.error = message
            log.error("serial link: %s", message)
        self.closed = True
        self._stop.set()
        with self._reply_cond:
            self._reply_cond.notify_all()
        for fn in list(self._listeners):
            self._safe_call(fn, Event("DISCONNECTED", {"error": message}))

    @staticmethod
    def _safe_call(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - one bad listener must not kill the reader
            log.exception("listener %r failed", fn)
