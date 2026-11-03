"""A stand-in for the Basler camera, for `vsbc gui --sim` and the tests.

It draws a speckle pattern that stretches with the crosshead extension the
GUI forwards (`set_state`), opens a crack across the middle after the break,
stamps frames with its own clock (offset from the Pi's and drifting 40 ppm,
like a real camera's crystal) and has the timestamp latch. NumPy only.
"""

from __future__ import annotations

import threading
import time

import numpy as np

STRAIN_PER_MM = 0.02          # image stretch per mm of crosshead extension
CRACK_SPEED_PX_S = 4000.0


def _blur(a: np.ndarray, r: int) -> np.ndarray:
    """Box blur of radius r along both axes (cumulative sums, no SciPy)."""
    for axis in (0, 1):
        c = np.cumsum(np.pad(a, [(r + 1, r) if i == axis else (0, 0) for i in range(2)], mode="edge"),
                      axis=axis, dtype=np.float64)
        n = a.shape[axis]
        hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
        lo = np.take(c, np.arange(0, n), axis=axis)
        a = ((hi - lo) / (2 * r + 1)).astype(np.float32)
    return a


class SimCamera:
    model = "Simulated camera"
    serial = "SIM"
    has_latch = True

    def __init__(self, width: int = 640, height: int = 160, fps: float = 100.0, seed: int = 0,
                 drift_ppm: float = 40.0, offset_ns: int = 5_000_000_000):
        self.width, self.height = int(width), int(height)
        self.fps = float(fps)
        self.exposure_us = 1488.0
        self._drift = 1.0 + drift_ppm * 1e-6
        self._offset = int(offset_ns)
        self._t_start = time.perf_counter_ns()
        rng = np.random.default_rng(seed)
        dots = (rng.random((self.height * 2, self.width)) > 0.62).astype(np.float32)
        base = _blur(dots, 2)
        base = (base - base.min()) / max(float(np.ptp(base)), 1e-6)
        self._pattern = (35.0 + 190.0 * base).astype(np.float32)
        self._noise = [rng.normal(0.0, 2.0, (self.height, self.width)).astype(np.float32) for _ in range(4)]
        self._ext_mm = 0.0
        self._crack_t: float | None = None
        self._lock = threading.Lock()
        self._next = 0.0
        self._block = 0
        self.grabbing = False

    # ------------------------------------------------------------ clock --
    def camera_ns(self) -> int:
        return int((time.perf_counter_ns() - self._t_start) * self._drift) + self._offset

    def latch(self) -> int:
        return self.camera_ns()

    # ------------------------------------------------------------ state --
    def set_state(self, ext_mm: float, broken: bool) -> None:
        with self._lock:
            self._ext_mm = float(ext_mm) if np.isfinite(ext_mm) else 0.0
            if broken and self._crack_t is None:
                self._crack_t = time.perf_counter()
            elif not broken:
                self._crack_t = None

    # --------------------------------------------------------- grabbing --
    def start(self) -> None:
        self.grabbing = True
        self._next = time.perf_counter()

    def stop(self) -> None:
        self.grabbing = False

    def grab(self, timeout_s: float = 1.0):
        """(image, camera timestamp ns, block id), or None on timeout."""
        if not self.grabbing:
            time.sleep(min(timeout_s, 0.05))
            return None
        wait = self._next - time.perf_counter()
        if wait > timeout_s:
            time.sleep(timeout_s)
            return None
        if wait > 0:
            time.sleep(wait)
        period = 1.0 / self.fps
        self._next = max(self._next + period, time.perf_counter() - period)
        stamp = self.camera_ns()
        image = self._render()
        self._block += 1
        return image, stamp, self._block

    def _render(self) -> np.ndarray:
        with self._lock:
            strain = self._ext_mm * STRAIN_PER_MM
            crack_age = None if self._crack_t is None else time.perf_counter() - self._crack_t
        h, w = self.height, self.width
        centre = h / 2.0
        rows = np.arange(h, dtype=np.float32)
        src = np.clip(np.rint(h + (rows - centre) / (1.0 + strain)).astype(int), 0, 2 * h - 1)
        image = self._pattern[src] * (self.exposure_us / 1488.0) + self._noise[self._block % 4]
        if crack_age is not None:
            length = int(min(w, crack_age * CRACK_SPEED_PX_S))
            gap = int(1 + 4 * min(1.0, crack_age / 0.05))
            c = int(centre)
            image[c - gap:c + gap, :length] = 12.0
        return np.clip(image, 0, 255).astype(np.uint8)
