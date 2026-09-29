"""In-process stand-in for the Mega running vsbc_firmware.

It speaks protocol v1 (docs/protocol.md) through the same transport interface
as a pyserial port, so the GUI, the CLI, calibration and the tests all run
without hardware. The command handling and safety logic mirror
firmware/vsbc_firmware (commands.cpp, tester.cpp, motion.cpp): when you change
one, change the other.

The "physical" side is simple: a crosshead whose real travel per step can be
off by `lead_error`, an HX711 with Gaussian noise, optional hanging weights
(`set_external_load`) and a ductile specimen that is gripped at the start of
the first RUN and breaks at a set extension.
"""

from __future__ import annotations

import json
import math
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from vsbc import FIRMWARE_NAME, FIRMWARE_VERSION, PROTOCOL_VERSION

F_CPU = 16_000_000
STEPS_PER_REV = 400
SCREW_LEAD_MM = 5.0
NOMINAL_STEPS_PER_MM = STEPS_PER_REV / SCREW_LEAD_MM
MAX_STEP_RATE_HZ = 120.0 / 60.0 * STEPS_PER_REV
MIN_RATE_MM_S = 0.002
DEFAULT_RATE_MM_S = 0.5
TIMER_MAX_STEP_RATE_HZ = 20000.0
AVERAGE_SAMPLES = 16
MIN_CAL_COUNTS = 1000
LOADCELL_TIMEOUT_S = 0.5
PROGRESS_INTERVAL_S = 0.5
NO_LOADCELL_STREAM_S = 0.1
OVERLOAD_WARN_S = 1.0
HX711_MAX = 8388607
HX711_MIN = -8388608

NAN = math.nan

# name -> (default, lo, hi, digits, saved in EEPROM)
SETTING_KEYS = {
    "steps_per_mm": (NOMINAL_STEPS_PER_MM, NOMINAL_STEPS_PER_MM / 2, NOMINAL_STEPS_PER_MM * 2, 4, True),
    "load_scale": (0.0, -1e9, 1e9, 4, True),
    "load_offset": (0, -8388608, 8388607, 0, True),
    "max_load": (0.0, 0.0, 1e6, 3, True),
    "min_pos": (-100.0, -10000.0, 10000.0, 4, True),
    "max_pos": (100.0, -10000.0, 10000.0, 4, True),
    "dir_invert": (0, 0, 1, 0, True),
    "invert_b": (0, 0, 1, 0, True),
    "stop_load": (0.0, 0.0, 1e6, 3, False),
    "break_drop": (0.0, 0.0, 99.0, 1, False),
    "break_min": (0.0, 0.0, 1e6, 3, False),
    "watchdog_ms": (0, 0, 600000, 0, False),
}
INT_KEYS = {"load_offset", "dir_invert", "invert_b", "watchdog_ms"}

_COMPACT = re.compile(r"^([A-Za-z]+)([-+.0-9].*)$")


def lround(x: float) -> int:
    """C lround: halves round away from zero."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def timer_step_rate(step_hz: float) -> float | None:
    """Step rate Timer1 really produces for a requested rate (None if impossible)."""
    if not step_hz > 0 or step_hz > TIMER_MAX_STEP_RATE_HZ:
        return None
    for prescaler in (1, 8, 64, 256, 1024):
        ticks = int(F_CPU / (2.0 * prescaler * step_hz) + 0.5)
        if 1 <= ticks <= 65536:
            return F_CPU / (2.0 * prescaler * ticks)
    return None


def fmt(value: float, digits: int) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return f"{value:.{digits}f}"


@dataclass
class Specimen:
    """Load (N) vs extension (mm) of a ductile test piece."""

    stiffness_n_per_mm: float = 400.0
    yield_load_n: float = 300.0
    peak_load_n: float = 400.0
    extension_at_peak_mm: float = 3.0
    break_extension_mm: float = 4.0
    load_at_break_frac: float = 0.75
    broken: bool = False

    def load(self, ext: float) -> float:
        if self.broken:
            return 0.0
        if ext >= self.break_extension_mm:
            self.broken = True
            return 0.0
        k = self.stiffness_n_per_mm
        e_y = self.yield_load_n / k
        if ext <= e_y:
            return k * ext          # elastic (and linear in compression)
        if ext <= self.extension_at_peak_mm:
            u = (ext - e_y) / (self.extension_at_peak_mm - e_y)
            return self.yield_load_n + (self.peak_load_n - self.yield_load_n) * (1 - (1 - u) ** 2)
        u = (ext - self.extension_at_peak_mm) / (self.break_extension_mm - self.extension_at_peak_mm)
        return self.peak_load_n * (1 - (1 - self.load_at_break_frac) * u * u)


class SimulatedMega:
    """Transport-compatible fake Mega. Thread-safe."""

    def __init__(
        self,
        *,
        specimen: Specimen | None = None,
        sample_rate_hz: float = 80.0,
        boot_delay_s: float = 0.0,
        calibrated: bool = True,
        loadcell: bool = True,
        lead_error: float = 1.0,
        noise_counts: float = 30.0,
        eeprom_path: str | Path | None = None,
        seed: int | None = None,
    ):
        self.timeout = 0.1                    # like pyserial: readline() waits this long
        self._out: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._rx = ""
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._closed = threading.Event()

        # Physical world
        self.true_offset = 84213
        self.true_scale = 4194.304            # counts per newton
        self.noise_counts = noise_counts
        self.lead_error = lead_error          # real travel / nominal travel
        self.external_load_n = 0.0
        self.loadcell_connected = loadcell
        self._specimen_template = specimen or Specimen()
        self.specimen = replace(self._specimen_template)
        self.mount_mm: float | None = None    # gripped at the first RUN
        self.sample_rate_hz = sample_rate_hz

        # Firmware state
        self._eeprom_path = Path(eeprom_path) if eeprom_path else None
        self.eeprom = self._load_eeprom(calibrated)
        self.settings = dict(self.eeprom)
        self.params = {k: v[0] for k, v in SETTING_KEYS.items() if not v[4]}
        self.rate = DEFAULT_RATE_MM_S
        self.stream = False
        self.motor_mask = 3
        self.pos_steps = [0, 0]
        self.referenced = False
        self.move: dict | None = None
        self.last_raw = 0
        self.last_load = NAN
        self.filt_load = NAN
        self.hist: list[int] = []
        self.lc_alive = False
        self.last_sample_t: float | None = None
        self.avg: dict | None = None
        self.last_host = 0.0
        self.last_emit = 0.0
        self.last_overload_warn = -math.inf
        self.peak = NAN
        self.run_dir = 0
        self.start_mag = 0.0
        self.sat_at_start = False
        self.break_count = 0

        self.booted = False
        self._boot_at = self._now() + boot_delay_s
        self._thread = threading.Thread(target=self._run, name="vsbc-sim", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ transport --
    def write(self, data: bytes) -> int:
        if self._closed.is_set():
            raise OSError("simulated port is closed")
        with self._lock:
            if not self.booted:
                return len(data)          # the bootloader swallows early input
            self._rx += data.decode("ascii", errors="replace")
            while True:
                ends = [i for i in (self._rx.find("\n"), self._rx.find("\r")) if i >= 0]
                if not ends:
                    break
                line, self._rx = self._rx[: min(ends)], self._rx[min(ends) + 1:]
                if line.strip():
                    self._handle_line(line)
        return len(data)

    def readline(self) -> bytes:
        try:
            line = self._out.get(timeout=self.timeout)
        except queue.Empty:
            return b""
        return (line + "\r\n").encode("ascii")

    def close(self) -> None:
        self._closed.set()

    # ---------------------------------------------------------- sim controls --
    def set_external_load(self, newtons: float) -> None:
        """Hang a known load on the load cell (calibration)."""
        with self._lock:
            self.external_load_n = newtons

    def true_position_mm(self) -> float:
        """Where the crosshead really is (what calipers would read)."""
        with self._lock:
            return self._physical_mm(self._now())

    def remount_specimen(self, specimen: Specimen | None = None) -> None:
        """Grip a fresh specimen at the current crosshead position."""
        with self._lock:
            if specimen is not None:
                self._specimen_template = specimen
            self.specimen = replace(self._specimen_template, broken=False)
            self.mount_mm = self._physical_mm(self._now())

    def disconnect_loadcell(self) -> None:
        with self._lock:
            self.loadcell_connected = False

    def connect_loadcell(self) -> None:
        with self._lock:
            self.loadcell_connected = True

    # --------------------------------------------------------------- helpers --
    def _now(self) -> float:
        return time.monotonic()

    def _emit(self, line: str) -> None:
        self._out.put(line)

    def _load_eeprom(self, calibrated: bool) -> dict:
        data = {k: v[0] for k, v in SETTING_KEYS.items() if v[4]}
        if calibrated:
            data["load_scale"] = self.true_scale
            data["load_offset"] = self.true_offset
        if self._eeprom_path and self._eeprom_path.is_file():
            try:
                stored = json.loads(self._eeprom_path.read_text())
                data.update({k: v for k, v in stored.items() if k in data})
            except (OSError, ValueError):
                pass
        return data

    def _spm(self) -> float:
        return self.settings["steps_per_mm"]

    def _steps_done(self, now: float) -> int:
        m = self.move
        if m is None:
            return 0
        t = m["stopped_at"] if m["stopped_at"] is not None else now
        return max(0, min(int((t - m["t0"]) * m["step_hz"]), m["steps"]))

    def _live_steps(self, now: float) -> tuple[int, int]:
        a, b = self.pos_steps
        m = self.move
        if m is not None:
            d = m["dir"] * self._steps_done(now)
            if m["mask"] & 1:
                a = m["start"][0] + d
            if m["mask"] & 2:
                b = m["start"][1] + d
        return a, b

    def _position(self, now: float) -> float:
        a, b = self._live_steps(now)
        return (b if self.motor_mask == 2 else a) / self._spm()

    def _physical_mm(self, now: float) -> float:
        a, b = self._live_steps(now)
        return (a + b) / 2 / NOMINAL_STEPS_PER_MM * self.lead_error

    def _to_newtons(self, counts: int) -> float:
        scale = self.settings["load_scale"]
        if scale == 0 or not math.isfinite(scale):
            return NAN
        return (counts - self.settings["load_offset"]) / scale

    def _calibrated(self) -> bool:
        scale = self.settings["load_scale"]
        return scale != 0 and math.isfinite(scale)

    @staticmethod
    def _saturated(raw: int) -> bool:
        return raw >= HX711_MAX or raw <= HX711_MIN

    def _state_char(self) -> str:
        if self.move is None:
            return "I"
        return "R" if self.move["mode"] == "RUN" else "M"

    def _state_name(self) -> str:
        return {"I": "IDLE", "M": "MOVE", "R": "RUN"}[self._state_char()]

    # ------------------------------------------------------------- main loop --
    def _run(self) -> None:
        period = 1.0 / self.sample_rate_hz
        next_t = time.monotonic()
        while not self._closed.is_set():
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                if self._closed.wait(delay):
                    break
            else:
                next_t = time.monotonic()
            with self._lock:
                now = self._now()
                if not self.booted:
                    if now >= self._boot_at:
                        self._boot()
                    continue
                if self.loadcell_connected:
                    self._on_sample(self._read_adc(now), now)
                self._check_loadcell(now)
                self._check_watchdog(now)
                self._service_move(now)
                self._periodic_telemetry(now)

    def _boot(self) -> None:
        self.booted = True
        self.last_host = self._now()
        self._emit("")
        self._emit(f"# VSBC tensile tester firmware {FIRMWARE_VERSION} (simulator)")
        self._emit("# Type ? then Enter for the command list.")
        self._emit(f"EVT READY name={FIRMWARE_NAME} ver={FIRMWARE_VERSION} proto={PROTOCOL_VERSION}")

    def _read_adc(self, now: float) -> int:
        load = self.external_load_n
        if self.mount_mm is not None:
            load += self.specimen.load(self._physical_mm(now) - self.mount_mm)
        counts = self.true_offset + load * self.true_scale + self._rng.gauss(0.0, self.noise_counts)
        return int(max(HX711_MIN, min(HX711_MAX, round(counts))))

    # ---------------------------------------------------- tester.cpp mirror --
    def _on_sample(self, raw: int, now: float) -> None:
        self.last_raw = raw
        self.last_sample_t = now
        self.last_load = self._to_newtons(raw)
        self.hist = (self.hist + [raw])[-3:]
        self.filt_load = self._to_newtons(sorted(self.hist)[1] if len(self.hist) == 3 else raw)
        if not self.lc_alive:
            self.lc_alive = True
            self._emit("EVT LOADCELL state=OK")
        if self.avg is not None:
            self._collect_average(raw)
        if self.move is not None:
            self._check_safety(raw, now)
        else:
            self._check_idle_overload(now)
        if self.stream:
            self._emit_data(now)

    def _check_safety(self, raw: int, now: float) -> None:
        if self._saturated(raw) and not self.sat_at_start:
            self._stop("OVERLOAD", now)
            return
        if math.isnan(self.filt_load):
            return
        mag = abs(self.filt_load)
        max_load = self.settings["max_load"]
        if max_load > 0 and mag >= max_load and mag > self.start_mag + 0.01 * max_load:
            self._stop("OVERLOAD", now)
            return
        if self.move["mode"] != "RUN":
            return
        stop_load = self.params["stop_load"]
        if stop_load > 0 and mag >= stop_load:
            self._stop("LOAD", now)
            return
        v = self.run_dir * self.filt_load
        if math.isnan(self.peak) or v > self.peak:
            self.peak = v
        drop, minimum = self.params["break_drop"], self.params["break_min"]
        if drop > 0 and minimum > 0 and self.peak >= minimum and v < self.peak * (1 - drop / 100):
            self.break_count += 1
            if self.break_count >= 2:
                self._stop("BREAK", now)
        else:
            self.break_count = 0

    def _check_idle_overload(self, now: float) -> None:
        max_load = self.settings["max_load"]
        if max_load <= 0 or math.isnan(self.filt_load) or abs(self.filt_load) < max_load:
            return
        if now - self.last_overload_warn < OVERLOAD_WARN_S:
            return
        self.last_overload_warn = now
        self._emit(f"EVT OVERLOAD load={self.filt_load:.3f}")

    def _collect_average(self, raw: int) -> None:
        self.avg["samples"].append(raw)
        samples = self.avg["samples"]
        if len(samples) < AVERAGE_SAMPLES:
            return
        kind, known = self.avg["kind"], self.avg["known"]
        self.avg = None
        mean = sum(samples) / len(samples)
        noise = math.sqrt(sum((s - mean) ** 2 for s in samples) / (len(samples) - 1))
        if kind == "TARE":
            self.settings["load_offset"] = lround(mean)
            self._emit(f"OK TARE offset={self.settings['load_offset']} noise={noise:.1f}")
            return
        delta = mean - self.settings["load_offset"]
        if abs(delta) < MIN_CAL_COUNTS:
            self._emit("ERR CAL RANGE load change too small: TARE with no load first, then apply a larger known load")
            return
        self.settings["load_scale"] = delta / known
        self._emit(f"OK CAL scale={self.settings['load_scale']:.4f} known={known:.3f}")
        self._emit("# Calibration changed in RAM. SAVE to keep it.")

    def _check_loadcell(self, now: float) -> None:
        alive = self.last_sample_t is not None and now - self.last_sample_t <= LOADCELL_TIMEOUT_S
        if not self.lc_alive or alive:
            return
        self.lc_alive = False
        self.hist = []
        self.last_load = NAN
        self.filt_load = NAN
        self._emit("EVT LOADCELL state=LOST")
        if self.move is not None and self.move["mode"] == "RUN":
            self._stop("LOADCELL", now)
        if self.avg is not None:
            self._emit(f"ERR {self.avg['kind']} NOLOAD load cell stopped responding")
            self.avg = None

    def _check_watchdog(self, now: float) -> None:
        wd = self.params["watchdog_ms"]
        if wd and self.move is not None and (now - self.last_host) * 1000 > wd:
            self._stop("WATCHDOG", now)

    def _periodic_telemetry(self, now: float) -> None:
        if self.stream:
            if not self.lc_alive and now - self.last_emit >= NO_LOADCELL_STREAM_S:
                self._emit_data(now)
        elif self.move is not None and now - self.last_emit >= PROGRESS_INTERVAL_S:
            self._emit_data(now)

    def _emit_data(self, now: float) -> None:
        self.last_emit = now
        load = self.last_load if self.lc_alive else NAN
        raw = self.last_raw if self.lc_alive else 0
        t_ms = int((now - self._t0) * 1000)
        self._emit(f"D {t_ms} {self._position(now):.4f} {fmt(load, 3)} {raw} {self._state_char()}")

    # ----------------------------------------------------- motion.cpp mirror --
    def _start_move(self, target: float, mode: str) -> str | None:
        """Returns None, "LIMIT" or "RATE"."""
        spm = self._spm()
        step_hz = timer_step_rate(self.rate * spm)
        if step_hz is None:
            return "RATE"
        ref = 1 if self.motor_mask == 2 else 0
        cur_mm = self.pos_steps[ref] / spm
        if (target > self.settings["max_pos"] and target > cur_mm) or (
            target < self.settings["min_pos"] and target < cur_mm
        ):
            return "LIMIT"
        delta = lround(target * spm) - self.pos_steps[ref]
        steps = abs(delta)
        self.move = {
            "mode": mode,
            "steps": steps,
            "dir": 1 if delta >= 0 else -1,
            "start": list(self.pos_steps),
            "mask": self.motor_mask,
            "t0": self._now(),
            "step_hz": step_hz,
            "timeout": steps / step_hz + 1.0,
            "stopped_at": None,
            "reason": None,
        }
        if self.motor_mask != 3 and steps > 0:
            self.referenced = False
        return None

    def _stop(self, reason: str, now: float) -> None:
        m = self.move
        if m is None or m["stopped_at"] is not None or self._steps_done(now) >= m["steps"]:
            return
        m["stopped_at"] = now
        m["reason"] = reason

    def _service_move(self, now: float) -> None:
        m = self.move
        if m is None:
            return
        if m["stopped_at"] is None and self._steps_done(now) < m["steps"]:
            if now - m["t0"] <= m["timeout"]:
                return
            self._stop("TIMEOUT", now)
        done = self._steps_done(now)
        d = m["dir"] * done
        if m["mask"] & 1:
            self.pos_steps[0] = m["start"][0] + d
        if m["mask"] & 2:
            self.pos_steps[1] = m["start"][1] + d
        if m["stopped_at"] is not None:
            reason, t_end = m["reason"], m["stopped_at"]
        else:
            reason, t_end = "DONE", m["t0"] + m["steps"] / m["step_hz"]
        self.move = None
        peak = self.peak * self.run_dir if not math.isnan(self.peak) else NAN
        self._emit(
            f"EVT MOVE_END reason={reason} mode={m['mode']} pos={self._position(now):.4f} "
            f"moved={d / self._spm():.4f} time={t_end - m['t0']:.2f} peak={fmt(peak, 3)}"
        )

    # --------------------------------------------------- commands.cpp mirror --
    def _handle_line(self, line: str) -> None:
        now = self._now()
        self.last_host = now
        toks = line.split()[:4]
        match = _COMPACT.match(toks[0])
        if match and len(toks) < 4 and len(match.group(1)) < 12:
            toks = [match.group(1), match.group(2), *toks[1:]]
        word, args = toks[0], toks[1:]
        cmd = self._find(word)
        if cmd is None:
            if self.move is not None:
                self._stop("STOP", now)
            self._emit(f"ERR {word} UNKNOWN unknown command (? for help)")
            return
        name, handler, any_time = cmd
        if not any_time:
            if self.move is not None:
                self._stop("STOP", now)
                self._emit(f"ERR {name} BUSY motion stopped")
                return
            if self.avg is not None:
                self._emit(f"ERR {name} BUSY TARE/CAL averaging in progress")
                return
        handler(name, args)

    def _find(self, word: str):
        w = word.upper()
        for name, alias, handler, any_time in self._commands():
            if w == name or (alias and w == alias):
                return name, handler, any_time
        return None

    def _commands(self):
        return (
            ("HELP", "?", self._cmd_help, True),
            ("ID", None, self._cmd_id, True),
            ("STATUS", "S", self._cmd_status, True),
            ("GET", None, self._cmd_get, True),
            ("PING", None, self._cmd_ping, True),
            ("STREAM", None, self._cmd_stream, True),
            ("STOP", "X", self._cmd_stop, True),
            ("RATE", "V", self._cmd_rate, False),
            ("MOVE", "M", self._cmd_move, False),
            ("GOTO", "G", self._cmd_goto, False),
            ("HOME", "H", self._cmd_home, False),
            ("RUN", "R", self._cmd_run, False),
            ("ZERO", "Z", self._cmd_zero, False),
            ("TARE", "T", self._cmd_tare, False),
            ("CAL", None, self._cmd_cal, False),
            ("MOTORS", None, self._cmd_motors, False),
            ("SET", None, self._cmd_set, False),
            ("SAVE", None, self._cmd_save, False),
            ("DEFAULTS", None, self._cmd_defaults, False),
        )

    @staticmethod
    def _number(args: list[str]) -> float | None:
        if not args:
            return None
        try:
            v = float(args[0])
        except ValueError:
            return None
        return v if math.isfinite(v) else None

    def _need_number(self, name: str, args: list[str]) -> float | None:
        v = self._number(args)
        if v is None:
            self._emit(f"ERR {name} ARG expected a number")
        return v

    def _max_rate(self) -> float:
        return MAX_STEP_RATE_HZ / self._spm()

    def _rate_range_error(self, name: str) -> None:
        self._emit(f"ERR {name} RANGE rate must be {MIN_RATE_MM_S:.3f} to {self._max_rate():.3f} mm/s")

    def _set_rate(self, mm_s: float) -> bool:
        if self.move is not None or not mm_s >= MIN_RATE_MM_S or mm_s > self._max_rate() * 1.0001:
            return False
        if timer_step_rate(mm_s * self._spm()) is None:
            return False
        self.rate = mm_s
        return True

    def _actual_rate(self) -> float:
        hz = timer_step_rate(self.rate * self._spm())
        return hz / self._spm() if hz else 0.0

    def _cmd_help(self, name, args):
        for text in (
            f"VSBC tensile tester firmware {FIRMWARE_VERSION} (simulator)",
            "Commands: RATE MOVE GOTO HOME RUN STOP ZERO TARE CAL STATUS GET SET SAVE DEFAULTS STREAM MOTORS ID PING HELP",
            "Data: D <t_ms> <pos_mm> <load_N> <raw> <I|M|R>",
        ):
            self._emit(f"# {text}")
        self._emit(f"OK {name}")

    def _cmd_id(self, name, args):
        self._emit(f"OK {name} name={FIRMWARE_NAME} ver={FIRMWARE_VERSION} proto={PROTOCOL_VERSION} loadcell=HX711 sim=1")

    def _cmd_status(self, name, args):
        now = self._now()
        motors = {1: "A", 2: "B"}.get(self.motor_mask, "AB")
        sps = self.sample_rate_hz if self.lc_alive else 0.0
        self._emit(
            f"OK {name} state={self._state_name()} pos={self._position(now):.4f} "
            f"load={fmt(self.last_load if self.lc_alive else NAN, 3)} raw={self.last_raw if self.lc_alive else 0} "
            f"rate={self.rate:.4f} motors={motors} ref={int(self.referenced)} lc={int(self.lc_alive)} "
            f"cal={int(self._calibrated())} sps={sps:.1f} stream={int(self.stream)} drops=0 "
            f"t={int((now - self._t0) * 1000)}"
        )

    def _format_setting(self, key: str) -> str:
        value = self.settings[key] if key in self.settings else self.params[key]
        digits = SETTING_KEYS[key][3]
        if key in INT_KEYS:
            return f"{key}={int(value)}"
        return f"{key}={value:.{digits}f}"

    def _cmd_get(self, name, args):
        self._emit(f"OK {name} " + " ".join(self._format_setting(k) for k in SETTING_KEYS))

    def _cmd_ping(self, name, args):
        self._emit(f"OK {name}")

    def _cmd_stream(self, name, args):
        if args:
            a = args[0].upper()
            if a in ("ON", "1"):
                self.stream = True
            elif a in ("OFF", "0"):
                self.stream = False
            else:
                self._emit(f"ERR {name} ARG expected ON or OFF")
                return
            self.last_emit = self._now()
        self._emit(f"OK {name} on={int(self.stream)}")

    def _cmd_stop(self, name, args):
        self._stop("STOP", self._now())
        self._emit(f"OK {name}")

    def _cmd_rate(self, name, args):
        v = self._need_number(name, args)
        if v is None:
            return
        if not self._set_rate(v):
            self._rate_range_error(name)
            return
        self._emit(f"OK {name} rate={self.rate:.4f} actual={self._actual_rate():.4f} max={self._max_rate():.4f}")

    def _start_move_cmd(self, name: str, target: float, mode: str) -> None:
        now = self._now()
        start = self._position(now)
        spm = self._spm()
        target = lround(target * spm) / spm
        error = self._start_move(target, mode)
        if error == "LIMIT":
            self._emit(
                f"ERR {name} LIMIT target outside the soft limits "
                f"{self.settings['min_pos']:.3f} to {self.settings['max_pos']:.3f} mm"
            )
            return
        if error == "RATE":
            self._rate_range_error(name)
            return
        self.start_mag = 0.0 if math.isnan(self.filt_load) else abs(self.filt_load)
        self.sat_at_start = self.lc_alive and self._saturated(self.last_raw)
        self.peak = NAN
        self.run_dir = self.move["dir"]
        self.break_count = 0
        self.last_emit = now
        m = self.move
        self._emit(
            f"OK {name} from={start:.4f} target={target:.4f} steps={m['steps']} "
            f"rate={self._actual_rate():.4f} expected={m['steps'] / m['step_hz']:.2f}"
        )

    def _cmd_move(self, name, args):
        d = self._need_number(name, args)
        if d is not None:
            self._start_move_cmd(name, self._position(self._now()) + d, "MOVE")

    def _cmd_goto(self, name, args):
        t = self._need_number(name, args)
        if t is not None:
            self._start_move_cmd(name, t, "MOVE")

    def _cmd_home(self, name, args):
        self._start_move_cmd(name, 0.0, "MOVE")

    def _cmd_run(self, name, args):
        d = self._need_number(name, args)
        if d is None:
            return
        if d == 0:
            self._emit(f"ERR {name} ARG distance must not be 0")
        elif not self.lc_alive:
            self._emit(f"ERR {name} NOLOAD load cell not responding")
        elif not self._calibrated():
            self._emit(f"ERR {name} UNCAL load cell not calibrated (TARE, then CAL <N>)")
        elif not self.referenced:
            self._emit(f"ERR {name} NOREF position not referenced: ZERO at the reference position first")
        elif self.motor_mask != 3:
            self._emit(f"ERR {name} ARG RUN needs both motors (MOTORS AB)")
        else:
            if self.mount_mm is None or self.specimen.broken:
                self.remount_specimen()
            self._start_move_cmd(name, self._position(self._now()) + d, "RUN")

    def _cmd_zero(self, name, args):
        v = 0.0
        if args:
            v = self._number(args)
            if v is None:
                self._emit(f"ERR {name} ARG expected a number")
                return
        s = lround(v * self._spm())
        self.pos_steps = [s, s]
        self.referenced = True
        self._emit(f"OK {name} pos={self._position(self._now()):.4f}")

    def _start_average(self, name: str, kind: str, known: float) -> None:
        if not self.lc_alive:
            self._emit(f"ERR {name} NOLOAD load cell not responding")
            return
        self.avg = {"kind": kind, "known": known, "samples": []}
        self._emit("# Averaging load-cell samples...")

    def _cmd_tare(self, name, args):
        self._start_average(name, "TARE", 0.0)

    def _cmd_cal(self, name, args):
        known = self._need_number(name, args)
        if known is None:
            return
        if known == 0:
            self._emit(f"ERR {name} ARG known load must not be 0")
            return
        self._start_average(name, "CAL", known)

    def _cmd_motors(self, name, args):
        which = args[0].upper() if args else ""
        mask = {"AB": 3, "BA": 3, "A": 1, "B": 2}.get(which)
        if mask is None:
            self._emit(f"ERR {name} ARG expected AB, A or B")
            return
        self.motor_mask = mask
        motors = {1: "A", 2: "B"}.get(mask, "AB")
        self._emit(f"OK {name} motors={motors}")
        if mask != 3:
            self._emit("# Service mode: only one driver gets pulses. A move clears the position reference.")

    def _cmd_set(self, name, args):
        if len(args) < 2:
            self._emit(f"ERR {name} ARG usage: SET <key> <value> (GET lists the keys)")
            return
        key = args[0].lower()
        if key not in SETTING_KEYS:
            self._emit(f"ERR {name} ARG unknown key (GET lists them)")
            return
        try:
            v = float(args[1])
        except ValueError:
            v = math.nan
        if not math.isfinite(v):
            self._emit(f"ERR {name} ARG expected a number")
            return
        _default, lo, hi, _digits, saved = SETTING_KEYS[key]
        bad = v < lo or v > hi
        bad |= key in ("dir_invert", "invert_b") and v not in (0.0, 1.0)
        bad |= key == "watchdog_ms" and v != 0 and v < 100
        bad |= key == "min_pos" and v >= self.settings["max_pos"]
        bad |= key == "max_pos" and v <= self.settings["min_pos"]
        if bad:
            self._emit(f"ERR {name} RANGE value out of range")
            return
        value = lround(v) if key in INT_KEYS else v
        (self.settings if saved else self.params)[key] = value
        if key == "steps_per_mm":
            self._reapply_rate()
        self._emit(f"OK {name} {self._format_setting(key)}")

    def _reapply_rate(self) -> None:
        self._set_rate(min(self.rate, self._max_rate()))

    def _cmd_save(self, name, args):
        self.eeprom = {k: self.settings[k] for k in self.eeprom}
        if self._eeprom_path:
            try:
                self._eeprom_path.parent.mkdir(parents=True, exist_ok=True)
                self._eeprom_path.write_text(json.dumps(self.eeprom, indent=2))
            except OSError:
                pass
        self._emit(f"OK {name}")

    def _cmd_defaults(self, name, args):
        for key, (default, *_rest, saved) in SETTING_KEYS.items():
            if saved:
                self.settings[key] = default
        self._reapply_rate()
        self._emit(f"OK {name}")
        self._emit("# Defaults restored in RAM. SAVE to keep them.")
