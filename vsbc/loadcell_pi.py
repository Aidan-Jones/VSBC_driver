"""HX711 load-cell amplifier wired straight to the Raspberry Pi's GPIO pins.

The Pi only *reads* the amplifier. Every reading is forwarded to the Mega as
``LC <raw>`` (see docs/protocol.md), and the Mega keeps doing the tare, the
calibration and the overload / break stops, exactly as when the HX711 is wired
to the Mega. If the readings stop (Pi frozen, amplifier unplugged), the Mega
sees "load cell lost" after 0.5 s and ends a running test by itself.

Timing: the HX711 powers down when SCK stays high for more than 60 us, and
Linux can pause a Python thread for longer than that at any moment. A paused
clock pulse corrupts the reading, so `PiHX711.read` times every pulse and
throws the reading away when one took too long. `vsbc loadcell` shows how
often that happens on a given Pi.

GPIO access uses lgpio, which works on the Pi 5 (RPi.GPIO does not).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

# Clock pulses after the 24 data bits select the next conversion's input.
GAIN_PULSES = {128: 1, 32: 2, 64: 3}   # 128 and 64: channel A; 32: channel B

# The HX711 powers down after 60 us of SCK high. A pulse measured longer than
# this is treated as a corrupted reading (the measurement includes call overhead).
MAX_PULSE_NS = 50_000

RP1_LABEL = "pinctrl-rp1"   # the Pi 5's 40-pin header


class LoadCellUnavailable(RuntimeError):
    """The Pi's GPIO pins could not be opened (not a Pi, lgpio missing, pins busy)."""


def _open_chip(lgpio):
    """Handle of the gpiochip behind the 40-pin header.

    On the Pi 5 that is the RP1 chip, whose number depends on the kernel
    (gpiochip4 on older Bookworm kernels, gpiochip0 on newer ones). On earlier
    Pis it is gpiochip0.
    """
    opened = []
    for number in range(8):
        try:
            handle = lgpio.gpiochip_open(number)
        except Exception:  # noqa: BLE001 - lgpio.error, or a missing chip
            continue
        try:
            label = str(lgpio.gpio_get_chip_info(handle)[3])
        except Exception:  # noqa: BLE001
            label = ""
        if RP1_LABEL in label:
            for other, _ in opened:
                lgpio.gpiochip_close(other)
            return handle
        opened.append((handle, number))
    if not opened:
        raise LoadCellUnavailable("no GPIO chip could be opened (is this a Raspberry Pi, and is the user in the 'gpio' group?)")
    for other, _ in opened[1:]:
        lgpio.gpiochip_close(other)
    return opened[0][0]


class PiHX711:
    """One HX711 on two GPIO pins (BCM numbers)."""

    def __init__(self, dout_pin: int = 5, sck_pin: int = 6, gain: int = 128, *, lgpio=None):
        if gain not in GAIN_PULSES:
            raise ValueError(f"HX711 gain must be one of {sorted(GAIN_PULSES)}, not {gain}")
        if lgpio is None:
            try:
                import lgpio
            except ImportError as e:
                raise LoadCellUnavailable(f"the lgpio module is not installed ({e})") from None
        self._gpio = lgpio
        self.dout_pin = dout_pin
        self.sck_pin = sck_pin
        self._pulses = GAIN_PULSES[gain]
        self._skip = 0
        self.discarded = 0        # readings thrown away (slow pulse, or no answer)
        self._handle = _open_chip(lgpio)
        try:
            # SCK low first: SCK high for more than 60 us powers the HX711 down.
            lgpio.gpio_claim_output(self._handle, sck_pin, 0)
            # Pull-up: with no HX711 connected DOUT reads high, which means "not ready".
            lgpio.gpio_claim_input(self._handle, dout_pin, getattr(lgpio, "SET_PULL_UP", 0))
        except Exception as e:  # noqa: BLE001 - lgpio.error
            self.close()
            raise LoadCellUnavailable(f"could not claim GPIO{dout_pin} / GPIO{sck_pin}: {e}") from None

    def _pulse(self) -> tuple[int, int]:
        """One clock pulse: (DOUT level while SCK was high, pulse duration in ns)."""
        gpio, handle = self._gpio, self._handle
        start = time.perf_counter_ns()
        gpio.gpio_write(handle, self.sck_pin, 1)
        bit = gpio.gpio_read(handle, self.dout_pin)
        gpio.gpio_write(handle, self.sck_pin, 0)
        return bit, time.perf_counter_ns() - start

    def read(self, timeout: float = 0.2) -> int | None:
        """The next conversion as signed 24-bit counts, or None.

        None means no conversion became ready within `timeout`, or the reading
        was discarded because it could not be trusted.
        """
        gpio, handle = self._gpio, self._handle
        deadline = time.monotonic() + timeout
        while gpio.gpio_read(handle, self.dout_pin) != 0:   # DOUT low = conversion ready
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.0005)

        value = 0
        slowest = 0
        for _ in range(24):
            bit, took = self._pulse()
            value = (value << 1) | (1 if bit else 0)
            slowest = max(slowest, took)
        for _ in range(self._pulses):
            slowest = max(slowest, self._pulse()[1])

        if slowest > MAX_PULSE_NS:
            # The HX711 may have powered down and restarted at its default gain:
            # this reading is corrupt and the next one is not trustworthy either.
            self.discarded += 1
            self._skip = 1
            return None
        # After the gain pulses the HX711 drives DOUT high until the next
        # conversion. Still low = nothing answered.
        if gpio.gpio_read(handle, self.dout_pin) == 0:
            self.discarded += 1
            return None
        if self._skip:
            self._skip -= 1
            self.discarded += 1
            return None
        if value & 0x800000:
            value -= 0x1000000   # sign-extend 24-bit two's complement
        return value

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        for pin in (self.sck_pin, self.dout_pin):
            try:
                self._gpio.gpio_free(handle, pin)
            except Exception:  # noqa: BLE001 - never claimed
                pass
        try:
            self._gpio.gpiochip_close(handle)
        except Exception:  # noqa: BLE001
            pass


class LoadCellFeeder:
    """Thread that reads a load-cell reader and passes every reading to `sink`.

    `reader` is anything with ``read() -> int | None`` and ``close()``.
    """

    def __init__(self, reader, sink: Callable[[int], None]):
        self.reader = reader
        self._sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vsbc-loadcell", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.reader.read()
                if raw is None:
                    continue
                self._sink(raw)
                self.samples += 1
            except Exception as e:  # noqa: BLE001 - GPIO failure, or the serial link closed
                if not self._stop.is_set():
                    self.error = str(e)
                    log.error("load-cell reader stopped: %s", e)
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self.reader.close()
