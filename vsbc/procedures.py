"""Test procedures. Only pull-to-failure for now; cyclic, hold and compression
tests can follow the same pattern (RUN with a negative distance already works).
"""

from __future__ import annotations

import logging
import math
import platform
import queue
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable

from vsbc import __version__
from vsbc.analysis import Summary
from vsbc.calibration import latest_record
from vsbc.config import Config, TestDefaults
from vsbc.link import DeviceError, LinkClosed
from vsbc.machine import Machine, mm_min_to_mm_s
from vsbc.protocol import Event, Reply, Sample
from vsbc.recorder import TestRecorder

log = logging.getLogger(__name__)

MIN_RATE_MM_S = 0.002


class PreflightError(RuntimeError):
    """The test cannot start; the message lists every reason, one per line."""


@dataclass
class PullTestSpec:
    specimen_id: str
    rate_mm_min: float = 5.0
    max_extension_mm: float = 50.0
    stop_load_n: float = 0.0          # 0 = off
    break_drop_pct: float = 40.0      # 0 = no break detection
    break_min_n: float = 5.0
    area_mm2: float | None = None
    gauge_length_mm: float | None = None
    tare_before: bool = True
    return_after: bool = False
    return_rate_mm_s: float = 2.0
    operator: str = ""
    notes: str = ""

    @classmethod
    def from_defaults(cls, specimen_id: str, defaults: TestDefaults, **overrides) -> "PullTestSpec":
        names = {f.name for f in fields(cls)}
        values = {k: v for k, v in asdict(defaults).items() if k in names}
        values["return_rate_mm_s"] = defaults.jog_rate_mm_s
        values.update(overrides)
        return cls(specimen_id=specimen_id, **values)


@dataclass
class PullTestResult:
    folder: Path | None
    stop_reason: str | None
    summary: Summary | None
    aborted: bool
    error: str | None = None
    move_end: dict | None = None


class PullTestRunner:
    """Runs one pull-to-failure test. `run()` blocks; call it on a worker thread
    from a GUI. `request_stop()` may be called from any thread."""

    def __init__(self, machine: Machine, spec: PullTestSpec, config: Config | None = None,
                 on_sample: Callable[[Sample, float], None] | None = None,
                 on_started: Callable[[float, Path], None] | None = None):
        self.machine = machine
        self.spec = spec
        self.config = config or machine.config
        self.on_sample = on_sample          # (sample, extension_mm), on the runner thread
        self.on_started = on_started        # (start_position_mm, folder)
        self.start_pos: float | None = None
        self.recorder: TestRecorder | None = None
        self.running = False
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        self._stop_requested.set()
        try:
            self.machine.stop()
        except LinkClosed:
            pass

    def preflight(self) -> list[str]:
        """Raises PreflightError if the test cannot start; returns warnings otherwise."""
        m, s = self.machine, self.spec
        if not m.connected:
            raise PreflightError("Not connected.")
        st = m.status()
        settings = m.refresh_settings()
        errors = []
        if st.state != "IDLE":
            errors.append("The machine is moving.")
        if not s.specimen_id.strip():
            errors.append("Enter a specimen ID.")
        if not st.loadcell_ok:
            errors.append("Load cell not responding.")
        elif not st.calibrated:
            errors.append("Load cell not calibrated (Calibrate tab).")
        if not st.referenced:
            errors.append("Position not referenced: set zero at the reference position (Jog tab).")
        if st.motors != "AB":
            errors.append("Both motors must be selected (motor select: AB).")
        rate = mm_min_to_mm_s(s.rate_mm_min)
        if not MIN_RATE_MM_S <= rate <= m.max_rate() * 1.0001:
            errors.append(f"Rate must be {MIN_RATE_MM_S * 60:.2f} to {m.max_rate() * 60:.0f} mm/min.")
        max_pos = settings.get("max_pos", math.inf)
        if s.max_extension_mm <= 0:
            errors.append("Max extension must be more than 0.")
        elif st.pos_mm + s.max_extension_mm > max_pos + 1e-6:
            errors.append(
                f"Start position {st.pos_mm:.2f} mm + max extension {s.max_extension_mm:.2f} mm "
                f"passes the upper travel limit ({max_pos:.2f} mm)."
            )
        if not 0 <= s.break_drop_pct < 100:
            errors.append("Break drop must be between 0 and 100 %.")
        for name in ("area_mm2", "gauge_length_mm"):
            value = getattr(s, name)
            if value is not None and value < 0:
                errors.append(f"{name} cannot be negative.")
        if errors:
            raise PreflightError("\n".join(errors))

        warnings = []
        max_load = settings.get("max_load", 0.0)
        if max_load <= 0:
            warnings.append("The machine overload cutoff (max_load) is off. Set it under Calibrate > Limits.")
        if s.break_drop_pct <= 0 or s.break_min_n <= 0:
            warnings.append("Break detection is off: the test runs until the max extension or a load limit.")
        if s.stop_load_n > 0 and max_load > 0 and s.stop_load_n > max_load:
            warnings.append(f"Stop load {s.stop_load_n:g} N is above the machine limit {max_load:g} N.")
        return warnings

    def run(self) -> PullTestResult:
        m, s = self.machine, self.spec
        self.running = True
        self._stop_requested.clear()
        inbox: queue.Queue = queue.Queue()
        rec: TestRecorder | None = None
        move_end: Event | None = None
        error: str | None = None
        aborted = False
        interrupted: BaseException | None = None
        try:
            if s.tare_before:
                m.tare()
            m.set("stop_load", s.stop_load_n or 0.0)
            m.set("break_drop", s.break_drop_pct or 0.0)
            m.set("break_min", s.break_min_n or 0.0)
            m.set_rate(mm_min_to_mm_s(s.rate_mm_min))
            m.refresh_settings()

            rec = TestRecorder(self.config.tests_dir, s.specimen_id, self._metadata(),
                               s.area_mm2, s.gauge_length_mm)
            self.recorder = rec
            m.add_raw_listener(rec.log_serial)
            m.add_listener(inbox.put)
            if self._stop_requested.is_set():
                raise InterruptedError("stopped before the test started")

            reply = m.run(s.max_extension_mm)
            self.start_pos = reply.number("from")
            rec.set_start_position(self.start_pos)
            if self.on_started:
                self.on_started(self.start_pos, rec.folder)
            move_end = self._record(inbox, rec, reply.number("expected", 0.0))
        except BaseException as e:  # noqa: BLE001 - always stop the machine and keep the data
            aborted = True
            error = f"{type(e).__name__}: {e}"
            if not isinstance(e, Exception):
                interrupted = e
            try:
                m.stop()
            except Exception:  # noqa: BLE001
                pass
            log.error("test aborted: %s", error)
        finally:
            m.remove_listener(inbox.put)
            if rec is not None:
                m.remove_raw_listener(rec.log_serial)

        stop_reason = move_end.fields.get("reason") if move_end else ("ERROR" if aborted else None)
        summary = None
        if rec is not None:
            summary = rec.finish(
                stop_reason,
                move_end=dict(move_end.fields) if move_end else None,
                aborted=aborted,
                error=error,
                break_drop_pct=s.break_drop_pct or 40.0,
                break_min_n=s.break_min_n,
            )
        self.running = False

        if s.return_after and not aborted and self.start_pos is not None and m.connected:
            try:
                m.move_and_wait(self.start_pos, rate_mm_s=s.return_rate_mm_s, absolute=True)
            except (DeviceError, TimeoutError, LinkClosed) as e:
                log.warning("return to start failed: %s", e)
        if interrupted is not None:
            raise interrupted
        return PullTestResult(rec.folder if rec else None, stop_reason, summary, aborted, error,
                              dict(move_end.fields) if move_end else None)

    def _record(self, inbox: queue.Queue, rec: TestRecorder, expected_s: float) -> Event:
        """Record samples between OK RUN and EVT MOVE_END."""
        m = self.machine
        deadline = time.monotonic() + expected_s + 30.0
        started = False
        stop_sent = False
        while True:
            if self._stop_requested.is_set() and not stop_sent:
                m.stop()
                stop_sent = True
            try:
                msg = inbox.get(timeout=0.2)
            except queue.Empty:
                if not m.connected:
                    raise LinkClosed("connection lost during the test") from None
                if time.monotonic() > deadline:
                    raise TimeoutError("the test did not finish in the expected time")
                continue
            if isinstance(msg, Reply):
                if msg.ok and msg.command == "RUN":
                    started = True
                continue
            if isinstance(msg, Event):
                if msg.name == "DISCONNECTED":
                    raise LinkClosed(msg.fields.get("error", "connection lost"))
                if started and msg.name == "MOVE_END":
                    return msg
                continue
            if started and isinstance(msg, Sample):
                rec.add(msg)
                if self.on_sample:
                    self.on_sample(msg, msg.pos_mm - (self.start_pos or 0.0))

    def _metadata(self) -> dict:
        m = self.machine
        cal = latest_record(self.config.calibration_dir, "load")
        return {
            "app_version": __version__,
            "test": "pull_to_failure",
            "specimen": asdict(self.spec),
            "firmware": asdict(m.info) if m.info else None,
            "port": m.port,
            "settings": m.settings,
            "load_calibration_record": str(cal[0]) if cal else None,
            "host": platform.node(),
        }
