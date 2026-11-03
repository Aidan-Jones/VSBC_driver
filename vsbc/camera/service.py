"""Camera process. Runs in the camera environment, started by vsbc.camera.client:

    python -m vsbc.camera.service --port N --camera-dir DIR [--sim]

(the connection key comes in the VSBC_CAMERA_KEY environment variable).

It opens the Basler camera with the user's capture_dic_live.py settings (or
the simulated camera), sends preview frames to the GUI, and records:

* every frame goes into a RAM ring of BUFFER_SECONDS;
* written as TIFF files: frame 0 (the DIC reference); the frame nearest each
  load sample (the GUI sends each sample's time, so data.csv rows and frames
  pair 1:1); and every frame of a full-rate window (high-speed mode, or the
  ring at the detected break);
* frames.csv lists every frame grabbed; camera_clock.csv holds the timestamp
  latches; capture_settings.json the settings and the windows.

Frames are saved in priority order (reference, then 1:1, then full-rate); when
the disk falls behind and the RAM budget fills, the oldest unsaved full-rate
frames are dropped first, so the frames nearest the break survive. Grabbing
never waits for the disk.

Messages are JSON over multiprocessing.connection (send_bytes), with images
as raw bytes in the next message. No pickle: the GUI and this process run
different NumPy versions. Imports: the standard library, NumPy, pypylon,
and the user's capture_dic_live.py.
"""

from __future__ import annotations

import argparse
import collections
import errno
import importlib.metadata
import json
import logging
import math
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Client
from pathlib import Path

import numpy as np

from vsbc.camera import tiff
from vsbc.camera.scripts import capture_settings, load_module

log = logging.getLogger("vsbc.camera.service")

KEY_ENV = "VSBC_CAMERA_KEY"
GB = 1024 ** 3
LOW_DISK_BYTES = 1 * GB           # below this only full-rate frames are saved
STOP_DISK_BYTES = 0.3 * GB        # below this the recording stops (and the GUI stops the test)
LATCH_EVERY_S = 2.0
NO_FRAMES_S = 3.0                 # a recording fails when the camera sends nothing for this long
NO_TARGETS_S = 1.5                # "match" falls back to every Nth frame without load samples
ENVELOPE_S = 2.0
ZOOM_SIZE = (500, 800)            # rows, columns of the 1:1 crop (as the script's 'z')


def now_ns() -> int:
    return time.perf_counter_ns()


def _json_default(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def shrink(image: np.ndarray, scale: float) -> np.ndarray:
    step = max(1, int(round(1.0 / scale)))
    if step == 1:
        return image
    h, w = (image.shape[0] // step) * step, (image.shape[1] // step) * step
    return image[:h, :w].reshape(h // step, step, w // step, step).mean(axis=(1, 3)).astype(np.uint8)


def memory_available() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class Link:
    """The connection to the GUI: one JSON message, then optionally one raw payload."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.closed = False

    def send(self, msg: dict, payload: bytes | None = None) -> None:
        if self.closed:
            return
        if payload is not None:
            msg = {**msg, "nbytes": len(payload)}
        data = json.dumps(msg, default=_json_default).encode("utf-8")
        with self.lock:
            try:
                self.conn.send_bytes(data)
                if payload is not None:
                    self.conn.send_bytes(payload)
            except (OSError, EOFError, ValueError):
                self.closed = True

    def recv(self) -> dict:
        return json.loads(self.conn.recv_bytes().decode("utf-8"))


# ------------------------------------------------------------- frame sources --

def _configure_builtin(camera, exposure_us, crop, fps):
    """Same setup as capture_dic_live.configure_camera, for a script without it."""
    def put(name, value):
        try:
            getattr(camera, name).Value = value
        except Exception as e:  # noqa: BLE001 - GenICam raises its own types
            log.info("%s not set: %s", name, e)

    def rounded(name, value):
        try:
            node = getattr(camera, name)
            inc = int(node.Inc)
            return min(int(node.Max), max(int(node.Min), int(value) // inc * inc))
        except Exception:  # noqa: BLE001
            return int(value)

    put("PixelFormat", "Mono8")
    put("ExposureAuto", "Off")
    put("GainAuto", "Off")
    put("Gain", 0.0)
    put("DeviceLinkThroughputLimitMode", "Off")
    if crop:
        put("OffsetX", 0)
        put("OffsetY", 0)
        put("Width", rounded("Width", crop["width"]))
        put("Height", rounded("Height", crop["height"]))
        put("OffsetX", rounded("OffsetX", crop["offset_x"]))
        put("OffsetY", rounded("OffsetY", crop["offset_y"]))
    put("ExposureTime", float(exposure_us))
    if fps is None:
        put("AcquisitionFrameRateEnable", False)
    else:
        put("AcquisitionFrameRateEnable", True)
        put("AcquisitionFrameRate", float(fps))
    return camera.Width.Value, camera.Height.Value


class PylonSource:
    """The Basler camera. Only the grab thread touches it."""

    def __init__(self, module):
        from pypylon import genicam, pylon

        self._pylon = pylon
        self._genicam = genicam
        self._module = module
        if module is not None and hasattr(module, "open_camera"):
            self.camera = module.open_camera()
        else:
            self.camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
            self.camera.Open()
        info = self.camera.GetDeviceInfo()
        self.model, self.serial = info.GetModelName(), info.GetSerialNumber()
        self.has_latch = self._available("TimestampLatch") and self._available("TimestampLatchValue")
        self.grab_errors = 0

    def _available(self, name: str) -> bool:
        try:
            return bool(self._genicam.IsAvailable(getattr(self.camera, name)))
        except Exception:  # noqa: BLE001 - no such node on this model
            return False

    def get(self, *names):
        for name in names:
            try:
                return getattr(self.camera, name).Value
            except Exception:  # noqa: BLE001
                continue
        return None

    def configure(self, exposure_us, crop, fps) -> tuple[int, int]:
        module = self._module
        if module is not None and hasattr(module, "configure_camera"):
            width, height = module.configure_camera(self.camera, exposure_us, crop, fps)
        else:
            width, height = _configure_builtin(self.camera, exposure_us, crop, fps)
        return int(width), int(height)

    def set_exposure(self, us: float) -> float:
        try:
            self.camera.ExposureTime.Value = float(us)
        except Exception as e:  # noqa: BLE001
            log.warning("exposure not set: %s", e)
        return float(self.get("ExposureTime") or us)

    def fps(self) -> float | None:
        return self.get("BslResultingAcquisitionFrameRate", "ResultingFrameRate")

    def temperature(self) -> float | None:
        return self.get("DeviceTemperature")

    def offsets(self):
        return self.get("OffsetX"), self.get("OffsetY")

    def start(self, latest_only: bool) -> None:
        p = self._pylon
        if self.camera.IsGrabbing():
            self.camera.StopGrabbing()
        try:
            self.camera.MaxNumBuffer.Value = 300
        except Exception:  # noqa: BLE001
            pass
        self.camera.StartGrabbing(p.GrabStrategy_LatestImageOnly if latest_only else p.GrabStrategy_OneByOne)

    def stop(self) -> None:
        if self.camera.IsGrabbing():
            self.camera.StopGrabbing()

    def grab(self, timeout_s: float):
        if not self.camera.IsGrabbing():
            time.sleep(timeout_s)
            return None
        res = self.camera.RetrieveResult(int(timeout_s * 1000), self._pylon.TimeoutHandling_Return)
        if res is None or not res.IsValid():
            return None
        try:
            if not res.GrabSucceeded():
                self.grab_errors += 1
                return None
            image = np.array(res.Array, copy=True)
            stamp = int(res.TimeStamp)
            try:
                block = int(res.BlockID)
            except Exception:  # noqa: BLE001
                block = -1
        finally:
            res.Release()
        return image, stamp, block

    def latch(self):
        """(Pi before, camera clock, Pi after). Only reads the camera's clock."""
        if not self.has_latch:
            return None
        before = now_ns()
        self.camera.TimestampLatch.Execute()
        after = now_ns()
        return before, int(self.camera.TimestampLatchValue.Value), after

    def close(self) -> None:
        try:
            self.stop()
            self.camera.Close()
        except Exception:  # noqa: BLE001
            pass


class SimSource:
    """vsbc.camera.simcam behind the same interface."""

    def __init__(self, size=(640, 160), fps: float = 100.0):
        from vsbc.camera.simcam import SimCamera

        self.sim = SimCamera(size[0], size[1], fps)
        self.model, self.serial, self.has_latch = self.sim.model, self.sim.serial, True
        self.grab_errors = 0

    def get(self, *names):
        return None

    def configure(self, exposure_us, crop, fps):
        self.sim.exposure_us = float(exposure_us)
        return self.sim.width, self.sim.height

    def set_exposure(self, us: float) -> float:
        self.sim.exposure_us = float(us)
        return self.sim.exposure_us

    def fps(self):
        return self.sim.fps

    def temperature(self):
        return 35.0

    def offsets(self):
        return 0, 0

    def start(self, latest_only: bool) -> None:
        self.sim.start()

    def stop(self) -> None:
        self.sim.stop()

    def grab(self, timeout_s: float):
        return self.sim.grab(timeout_s)

    def latch(self):
        before = now_ns()
        stamp = self.sim.latch()
        return before, stamp, now_ns()

    def close(self) -> None:
        self.sim.stop()


# ----------------------------------------------------------------- save queue --

class SaveQueue:
    """Frames waiting to be written. Class 0 = reference, 1 = routine / 1:1, 2 = full-rate.

    Writers take the lowest class first. Over the RAM budget the oldest
    full-rate frames are dropped first (the newest are nearest the break);
    protected ones (also wanted as 1:1 frames) move to class 1 instead.
    """

    def __init__(self, budget_bytes: int):
        self.cv = threading.Condition()
        self.queues = (collections.deque(), collections.deque(), collections.deque())
        self.bytes = 0
        self.peak = 0
        self.budget = budget_bytes
        self.in_flight = 0
        self.closed = False
        self.protected: set[int] = set()
        self.dropped: list[int] = []      # full-rate frames given up for RAM
        self.skipped: list[int] = []      # routine frames that did not fit

    def put(self, idx: int, image: np.ndarray, cls: int) -> bool:
        need = image.nbytes
        with self.cv:
            full = self.queues[2]
            while self.bytes + need > self.budget and full:
                old_idx, old = full.popleft()
                if old_idx in self.protected:
                    self.queues[1].append((old_idx, old))
                    continue
                self.bytes -= old.nbytes
                self.dropped.append(old_idx)
            if self.bytes + need > self.budget and cls != 0:
                (self.dropped if cls == 2 else self.skipped).append(idx)
                return False
            self.queues[cls].append((idx, image))
            self.bytes += need
            self.peak = max(self.peak, self.pending_locked())
            self.cv.notify()
            return True

    def protect(self, idx: int) -> None:
        with self.cv:
            self.protected.add(idx)

    def get(self, timeout: float):
        with self.cv:
            if not any(self.queues) and not self.closed:
                self.cv.wait(timeout)
            for q in self.queues:
                if q:
                    idx, image = q.popleft()
                    self.bytes -= image.nbytes
                    self.in_flight += 1
                    return idx, image
            return None

    def task_done(self) -> None:
        with self.cv:
            self.in_flight -= 1
            self.cv.notify_all()

    def pending_locked(self) -> int:
        return sum(len(q) for q in self.queues) + self.in_flight

    def pending(self) -> int:
        with self.cv:
            return self.pending_locked()

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify_all()


# -------------------------------------------------------------------- live DIC --

class LiveDic:
    """The user's dic_worker in its own process, fed the frames as they are written."""

    def __init__(self, camera_dir: Path, cfg: dict, link: Link):
        import multiprocessing as mp

        from vsbc.camera.jobs import dic_process_entry

        ctx = mp.get_context("spawn")
        self.done_q, self.status_q = ctx.Queue(), ctx.Queue()
        self.proc = ctx.Process(target=dic_process_entry, args=(str(camera_dir), cfg, self.done_q, self.status_q),
                                daemon=True)
        self.proc.start()
        self.link = link
        threading.Thread(target=self._relay, name="live-dic-status", daemon=True).start()

    def notify(self, idx: int) -> None:
        self.done_q.put(int(idx))

    def finish(self, window_frames: list[int]) -> None:
        for i in window_frames:
            self.done_q.put(int(i))
        if window_frames:
            self.done_q.put(("crack", [int(i) for i in window_frames]))
        self.done_q.put(None)

    def _relay(self) -> None:
        import queue as q

        while True:
            try:
                msg = self.status_q.get(timeout=1.0)
            except q.Empty:
                if not self.proc.is_alive():
                    return
                continue
            self.link.send({"ev": "dic", "msg": list(msg)})
            if msg and msg[0] == "done":
                return


# ------------------------------------------------------------------- recording --

@dataclass
class Frame:
    idx: int
    cam_ns: int
    host_ns: int
    image: np.ndarray


class Recording:
    """One recording. on_frame / periodic / close run on the grab thread; the
    request_* methods and add_targets may be called from the command thread."""

    def __init__(self, service: "Service", opts: dict):
        self.service = service
        self.link = service.link
        self.opts = opts
        self.folder = Path(opts["folder"])
        self.images = self.folder / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        (self.folder / "pyvale_results").mkdir(exist_ok=True)
        s = service.settings
        self.mode = opts.get("mode", "match")
        self.every = max(1, int(opts.get("every") or s["SAVE_EVERY"]))
        self.divisor = max(1, int(opts.get("divisor") or 1))
        self.buffer_s = float(opts.get("buffer_s") or s["BUFFER_SECONDS"])
        self.max_s = opts.get("max_s")
        self.load_rate = float(opts.get("load_rate_hz") or 0.0) or 10.0
        self.fps = float(service.fps or 100.0)
        self.period_ns = 1e9 / self.fps
        self.width, self.height = service.width, service.height
        frame_bytes = self.width * self.height
        self.ring: collections.deque[Frame] = collections.deque(maxlen=max(1, int(self.buffer_s * self.fps)))
        ram = memory_available()
        budget = 0.5 * ram if ram else 1.5 * GB
        self.queue = SaveQueue(int(max(budget, 50 * frame_bytes)))

        self.rows: list[list] = []
        self.idx = 0
        self.t0_ns: int | None = None
        self.started_iso = datetime.now().isoformat(timespec="seconds")
        # Camera nodes are only read on the grab thread (begin / close).
        self.temp_start = self.temp_end = None
        self.offsets = (None, None)
        self.enqueued: set[int] = set()
        self.saved: set[int] = set()
        self.matched: set[int] = set()
        self.write_errors: list[int] = []
        self.windows: list[list[int]] = []
        self.window_labels: list[str] = []
        self.window_start: int | None = None
        self.high_speed = False
        self._hs_request: bool | None = None
        self.markers: list[dict] = []
        self.pending_marker = ""
        self._targets: list[int] = []
        self._targets_lock = threading.Lock()
        self._target_count = 0
        self.last_target_mono: float | None = None
        self.missed_targets = 0
        self.skipped_low_disk = 0
        self.latches: list[tuple[int, int, int]] = []
        self.last_latch = 0.0
        self.offset_ns: float | None = None
        self._envelope: collections.deque = collections.deque()
        self.latency_ns = float(service.exposure) * 1e3 + frame_bytes / 350e6 * 1e9
        self.low_disk = False
        self.last_disk_check = 0.0
        self.stop_request: tuple[str, bool] | None = None
        self.failed: str | None = None
        self.closed = False
        self.reference_saved = threading.Event()
        self.bytes_written = 0
        self._saved_lock = threading.Lock()
        self._routine_saved = 0
        self.started_mono = time.monotonic()

        self.dic = None
        dic_opts = opts.get("live_dic")
        self.overview_every = max(1, int((dic_opts or {}).get("overview_every") or 3))
        self.crack_every = max(1, int((dic_opts or {}).get("crack_every") or s["CRACK_DIC_STEP"]))
        if dic_opts and service.module is not None and hasattr(service.module, "dic_worker"):
            roi = np.load(dic_opts["roi"])
            cfg = dict(frames_dir=str(self.images), out_dir=str(self.folder / "pyvale_results"), step=1,
                       roi_mask=roi["mask"].astype(bool), roi_seed=[int(v) for v in roi["seed"]],
                       subset_size=int(dic_opts.get("subset_size") or s["subset_size"]),
                       subset_step=int(dic_opts.get("subset_step") or s["subset_step"]),
                       max_displacement=int(dic_opts.get("max_displacement") or s["max_displacement"]),
                       threads=int(dic_opts.get("threads") or s["DIC_THREADS"]),
                       fill_batch=int(s["FILL_BATCH"]), window=False)
            try:
                self.dic = LiveDic(service.camera_dir, cfg, self.link)
            except Exception as e:  # noqa: BLE001
                self.link.send({"ev": "error", "fatal": False, "message": f"Live DIC did not start: {e}"})

        n_writers = max(1, int(s["N_WRITERS"]))
        self.writers = [threading.Thread(target=self._writer, name=f"frame-writer-{i}", daemon=True)
                        for i in range(n_writers)]
        for w in self.writers:
            w.start()

    # -------------------------------------------------------- from the GUI --
    def add_targets(self, times_ns) -> None:
        """Pi times of new load samples: save the frame nearest each one."""
        with self._targets_lock:
            for t in times_ns:
                self._target_count += 1
                if self._target_count % self.divisor == 0:
                    self._targets.append(int(t))
            self.last_target_mono = time.monotonic()

    def request_high_speed(self, on: bool) -> None:
        self._hs_request = on

    def mark(self, label: str | None = None) -> None:
        label = label or f"marker{len([m for m in self.markers if m['label'].startswith('marker')]) + 1}"
        self.pending_marker = label
        self.markers.append({"frame": self.idx, "pc_time_s": self._elapsed(), "label": label})

    def request_stop(self, reason: str, dump: bool = False) -> None:
        if self.stop_request is None:
            self.stop_request = (reason, dump)

    def fail(self, reason: str) -> None:
        """Something makes recording impossible: stop, and tell the GUI (which stops the test)."""
        if self.failed is None:
            self.failed = reason
            log.error("recording failed: %s", reason)
            self.link.send({"ev": "stopped_itself", "reason": reason})
        self.request_stop(reason, dump=False)

    # ------------------------------------------------------- grab thread --
    def begin(self, source) -> None:
        self.temp_start = source.temperature()
        self.offsets = source.offsets()
        self._latch(source)

    def on_frame(self, image: np.ndarray, cam_ns: int, block: int, host_ns: int) -> None:
        if self.t0_ns is None:
            self.t0_ns = host_ns
        frame = Frame(self.idx, cam_ns, host_ns, image)
        self.idx += 1
        self._update_offset(frame)
        if self._hs_request is not None:
            on, self._hs_request = self._hs_request, None
            if on and not self.high_speed:
                self._open_window("high speed")
            elif not on and self.high_speed:
                self._close_window()
        self.rows.append([frame.idx, block, cam_ns, host_ns, self.pending_marker])
        self.pending_marker = ""
        self.ring.append(frame)
        cls = self._policy(frame.idx)
        if cls is not None:
            self._enqueue(frame, cls)
        self._match_targets()

    def periodic(self, source, mono: float) -> None:
        if mono - self.last_latch >= LATCH_EVERY_S:
            self._latch(source)
        if mono - self.last_disk_check >= 1.0:
            self.last_disk_check = mono
            try:
                free = shutil.disk_usage(self.folder).free
            except OSError:
                free = None
            if free is not None:
                if free < STOP_DISK_BYTES:
                    self.fail(f"the disk is full ({free / GB:.2f} GB left)")
                elif free < LOW_DISK_BYTES and not self.low_disk:
                    self.low_disk = True
                    self.link.send({"ev": "error", "fatal": False,
                                    "message": "Disk almost full: only full-rate frames are saved now."})
        if self.max_s and mono - self.started_mono > float(self.max_s) and self.stop_request is None:
            self.link.send({"ev": "stopped_itself", "reason": "the maximum recording length was reached"})
            self.request_stop("maximum length", dump=False)

    def close(self, source=None) -> None:
        """The stop request, on the grab thread: dump the ring if asked, close the window."""
        reason, dump = self.stop_request
        if dump and not self.high_speed and self.ring:
            self._open_window("break")
        if self.high_speed:
            self._close_window()
        if source is not None:
            try:
                self.temp_end = source.temperature()
            except Exception:  # noqa: BLE001 - the camera may be gone
                pass
        self.closed = True
        self.reason = reason

    # ------------------------------------------------------------ policy --
    def _elapsed(self) -> float:
        return 0.0 if self.t0_ns is None else (now_ns() - self.t0_ns) / 1e9

    def no_samples(self) -> bool:
        """"match" without load samples arriving (no Mega): fall back to every Nth frame."""
        last = self.last_target_mono
        return self.mode == "match" and time.monotonic() - (last if last is not None else self.started_mono) > NO_TARGETS_S

    def _policy(self, idx: int) -> int | None:
        if idx == 0:
            return 0
        if self.high_speed:
            return 2
        if self.mode == "all":
            return 1
        if self.mode == "every":
            return 1 if idx % self.every == 0 else None
        # "match": the frames come from the load samples (_match_targets)
        if self.no_samples():
            n = max(1, int(round(self.fps / (self.load_rate / self.divisor))))
            return 1 if idx % n == 0 else None
        return None

    def _enqueue(self, frame: Frame, cls: int) -> None:
        if cls == 1 and self.low_disk:
            self.skipped_low_disk += 1
            return
        if frame.idx in self.enqueued:
            if cls < 2:
                self.queue.protect(frame.idx)
            return
        if self.queue.put(frame.idx, frame.image, cls):
            self.enqueued.add(frame.idx)

    def _open_window(self, label: str) -> None:
        self.high_speed = True
        self.window_start = self.ring[0].idx if self.ring else self.idx
        n = len(self.windows) + 1
        crack = f"crack{n}"
        if self.rows:
            self.rows[-1][4] = (self.rows[-1][4] + " " if self.rows[-1][4] else "") + crack
        self.markers.append({"frame": self.rows[-1][0] if self.rows else 0, "pc_time_s": self._elapsed(),
                             "label": crack})
        self.window_labels.append(label)
        for frame in list(self.ring):
            self._enqueue(frame, 2)
        self.link.send({"ev": "high_speed", "on": True, "label": label, "start": self.window_start})

    def _close_window(self) -> None:
        end = self.rows[-1][0] if self.rows else self.window_start
        self.windows.append([int(self.window_start), int(end)])
        self.high_speed = False
        self.window_start = None
        self.link.send({"ev": "high_speed", "on": False, "windows": self.windows})

    # ---------------------------------------------------- 1:1 with samples --
    def _update_offset(self, frame: Frame) -> None:
        """Online camera -> Pi offset: the latest latch, or the fastest recent arrivals."""
        if self.latches:
            return
        value = frame.host_ns - frame.cam_ns
        env = self._envelope
        while env and env[-1][1] >= value:
            env.pop()
        env.append((frame.host_ns, value))
        while env and env[0][0] < frame.host_ns - ENVELOPE_S * 1e9:
            env.popleft()
        self.offset_ns = env[0][1] - self.latency_ns

    def _latch(self, source) -> None:
        self.last_latch = time.monotonic()
        try:
            pair = source.latch()
        except Exception as e:  # noqa: BLE001
            log.warning("timestamp latch failed: %s", e)
            pair = None
        if pair is None:
            return
        before, cam, after = pair
        self.latches.append(pair)
        if after - before < 5_000_000:
            self.offset_ns = float(before + (after - before) // 2 - cam)

    def _frame_time(self, frame: Frame) -> float:
        return frame.cam_ns + self.offset_ns

    def _nearest(self, t: int) -> Frame | None:
        newer = None
        for frame in reversed(self.ring):
            ft = self._frame_time(frame)
            if ft <= t:
                if newer is not None and self._frame_time(newer) - t < t - ft:
                    return newer
                return frame
            newer = frame
        if newer is not None and self._frame_time(newer) - t <= self.period_ns:
            return newer
        return None

    def _match_targets(self) -> None:
        if self.offset_ns is None or not self.ring:
            return
        with self._targets_lock:
            if not self._targets:
                return
            targets, self._targets = self._targets, []
        newest = self._frame_time(self.ring[-1])
        later = []
        for t in targets:
            if t > newest + 0.5 * self.period_ns:
                later.append(t)            # its frame has not arrived yet
                continue
            frame = self._nearest(t)
            if frame is None:
                self.missed_targets += 1
                continue
            self.matched.add(frame.idx)
            self._enqueue(frame, 1)
        if later:
            with self._targets_lock:
                self._targets = later + self._targets

    # ------------------------------------------------------------ writers --
    def _writer(self) -> None:
        while True:
            item = self.queue.get(0.5)
            if item is None:
                if self.queue.closed:
                    return
                continue
            idx, image = item
            ok = True
            try:
                tiff.write_gray8(self.images / f"frame_{idx:06d}.tif", image)
            except OSError as e:
                ok = False
                if e.errno == errno.ENOSPC:
                    self.fail("the disk is full")
                else:
                    log.warning("frame %d not written: %s", idx, e)
            if ok:
                with self._saved_lock:
                    self.saved.add(idx)
                    self.bytes_written += image.nbytes
                    overview = False
                    if idx != 0 and self.window_of(idx) is None:
                        self._routine_saved += 1
                        overview = self._routine_saved % self.overview_every == 0
                if idx == 0:
                    self.reference_saved.set()
                    self.link.send({"ev": "reference_saved"})
                if self.dic is not None and (idx == 0 or overview):
                    self.dic.notify(idx)
            else:
                self.write_errors.append(idx)
            self.queue.task_done()

    def window_of(self, idx: int) -> int | None:
        for k, (start, end) in enumerate(self.windows):
            if start <= idx <= end:
                return k
        if self.window_start is not None and idx >= self.window_start:
            return len(self.windows)
        return None

    # ------------------------------------------------------------ finishing --
    def finalize(self) -> None:
        """After close(): wait for the writers, then write frames.csv and the settings (own thread)."""
        try:
            last_note = 0.0
            while self.queue.pending():
                if time.monotonic() - last_note >= 0.5:
                    last_note = time.monotonic()
                    self.link.send({"ev": "finishing", "left": self.queue.pending()})
                time.sleep(0.05)
            self.queue.close()
            for w in self.writers:
                w.join(5)
            self._write_files()
            window_frames = sorted(i for i in self.saved if self.window_of(i) is not None)
            if self.dic is not None:
                self.dic.finish(window_frames[::self.crack_every])
            self.link.send({"ev": "finished", "summary": self.summary()})
        except Exception as e:  # noqa: BLE001 - always report
            log.exception("finishing the recording failed")
            self.link.send({"ev": "finished", "summary": {**self.summary(), "error": str(e)}})

    def summary(self) -> dict:
        blocks = np.array([r[1] for r in self.rows], dtype=np.int64)
        dropped = int(np.sum(np.diff(blocks) - 1)) if blocks.size > 1 and blocks.min() >= 0 else None
        stamps = np.array([r[2] for r in self.rows], dtype=np.int64)
        fps = float(1e9 / np.median(np.diff(stamps))) if stamps.size > 1 else None
        return {
            "folder": str(self.folder), "reason": getattr(self, "reason", None), "failed": self.failed,
            "frames_grabbed": self.idx, "frames_saved": len(self.saved), "frames_matched": len(self.matched & self.saved),
            "camera_dropped_frames": dropped, "grab_errors": self.service.source.grab_errors if self.service.source else None,
            "write_errors": len(self.write_errors), "ram_dropped_frames": len(self.queue.dropped),
            "skipped_frames": len(self.queue.skipped) + self.skipped_low_disk, "missed_samples": self.missed_targets,
            "full_rate_windows": self.windows, "fps_measured": fps, "duration_s": self._elapsed(),
            "max_save_queue": self.queue.peak, "live_dic": self.dic is not None,
        }

    def _write_files(self) -> None:
        held = set()
        for start, end in self.windows:
            held.update(range(start, end + 1))
        t0 = self.t0_ns or 0
        with open(self.folder / "frames.csv", "w", newline="", encoding="utf-8") as f:
            f.write("frame_index,filename,saved,block_id,camera_timestamp_ns,pc_time_s,marker,host_ns,matched,full_rate\n")
            for idx, block, cam, host, marker in self.rows:
                f.write(f"{idx},frame_{idx:06d}.tif,{int(idx in self.saved)},{block},{cam},"
                        f"{(host - t0) / 1e9:.6f},{marker},{host},{int(idx in self.matched)},{int(idx in held)}\n")
        with open(self.folder / "camera_clock.csv", "w", newline="", encoding="utf-8") as f:
            f.write("host_before_ns,camera_ns,host_after_ns\n")
            for before, cam, after in self.latches:
                f.write(f"{before},{cam},{after}\n")
        svc, s = self.service, self.service.settings
        offset_x, offset_y = self.offsets
        summary = self.summary()
        settings = {
            "test_name": self.opts.get("name") or s["TEST_NAME"], "aperture": s["APERTURE_LABEL"],
            "started": self.started_iso, "status": "failed: " + self.failed if self.failed else "complete",
            "exposure_us": svc.exposure, "gain": 0.0, "fps_requested": s["FPS"], "fps_camera": svc.fps,
            "width": self.width, "height": self.height, "offset_x": offset_x, "offset_y": offset_y,
            "save_every": self.every, "buffer_seconds": self.buffer_s, "post_seconds": 0,
            "model": svc.source.model if svc.source else None, "serial": svc.source.serial if svc.source else None,
            "camera_temperature_c_start": self.temp_start,
            "camera_temperature_c_end": self.temp_end,
            "dic": dict(step=s["DIC_STEP"], subset_size=s["subset_size"], subset_step=s["subset_step"],
                        max_displacement=s["max_displacement"], threads=s["DIC_THREADS"]),
            "frames_grabbed": self.idx, "frames_saved": len(self.saved), "duration_s": summary["duration_s"],
            "fps_measured": summary["fps_measured"], "dropped_frames": summary["camera_dropped_frames"],
            "grab_errors": summary["grab_errors"], "write_errors": len(self.write_errors),
            "max_save_queue": self.queue.peak, "markers": self.markers,
            # Recorded by the vsbc GUI:
            "save_mode": self.mode, "match_divisor": self.divisor, "load_rate_hz": self.load_rate,
            "full_rate_windows": self.windows, "crack_windows": self.windows, "window_labels": self.window_labels,
            "frames_matched": summary["frames_matched"], "skipped_frames": summary["skipped_frames"],
            "ram_dropped_frames": summary["ram_dropped_frames"], "missed_samples": self.missed_targets,
            "timestamp_latch": bool(self.latches), "stop_reason": getattr(self, "reason", None),
            "test": bool(self.opts.get("test")), "units": self.opts.get("units"),
            "camera_script": str(svc.camera_dir / "capture_dic_live.py"),
        }
        (self.folder / "capture_settings.json").write_text(json.dumps(settings, indent=2, default=_json_default),
                                                           encoding="utf-8")


# ---------------------------------------------------------------------- service --

class Service:
    def __init__(self, link: Link, args):
        self.link = link
        self.args = args
        self.camera_dir = Path(args.camera_dir)
        try:
            self.module, self.script_error = load_module(self.camera_dir), None
        except Exception as e:  # noqa: BLE001 - the simulator still works without the scripts
            self.module, self.script_error = None, f"{type(e).__name__}: {e}"
            log.warning("capture_dic_live.py not loaded: %s", self.script_error)
        self.settings = capture_settings(self.module)
        self.exposure = float(self.settings["EXPOSURE_US"])
        self.source = None
        self.width = self.height = 0
        self.fps: float | None = None
        self.rec: Recording | None = None
        self.finalizers: list[threading.Thread] = []
        self.ops: list = []
        self.ops_lock = threading.Lock()
        self.quit = threading.Event()
        self.zoom = False
        self.preview_hz = 10.0
        self.preview_scale = float(self.settings.get("PREVIEW_SCALE") or 0.4)
        self.last_image: np.ndarray | None = None
        self._last_bytes = 0
        self._last_status = time.monotonic()

    # ------------------------------------------------------------- startup --
    def hello(self) -> None:
        versions = {"python": sys.version.split()[0], "numpy": np.__version__}
        # From the package metadata: importing pyvale here would load vtk, numba and Qt into the
        # process that grabs the frames.
        for name, dists in (("cv2", ("opencv-python", "opencv-python-headless", "opencv-contrib-python")),
                            ("pypylon", ("pypylon",)), ("pyvale", ("pyvale",))):
            versions[name] = None
            for dist in dists:
                try:
                    versions[name] = importlib.metadata.version(dist)
                    break
                except importlib.metadata.PackageNotFoundError:
                    continue
        self.link.send({"ev": "hello", "settings": self.settings, "script_error": self.script_error,
                        "sim": bool(self.args.sim), "versions": versions,
                        "has_dic_worker": self.module is not None and hasattr(self.module, "dic_worker"),
                        "has_configure": self.module is not None and hasattr(self.module, "configure_camera"),
                        "ram_available": memory_available()})

    def open(self) -> None:
        try:
            if self.args.sim:
                w, h = (int(v) for v in self.args.sim_size.lower().split("x"))
                self.source = SimSource((w, h), self.args.sim_fps)
            else:
                self.source = PylonSource(self.module)
            self.width, self.height = self.source.configure(self.exposure, self.settings.get("CROP"),
                                                            self.settings.get("FPS"))
            self.exposure = float(self.source.get("ExposureTime") or self.exposure)
            self.fps = self.source.fps()
            if not self.fps and not self.args.sim:
                self.fps = self._measure_fps()
            offset_x, offset_y = self.source.offsets()
            self.link.send({"ev": "opened", "model": self.source.model, "serial": self.source.serial,
                            "width": self.width, "height": self.height, "fps": self.fps,
                            "exposure_us": self.exposure, "offset_x": offset_x, "offset_y": offset_y,
                            "latch": self.source.has_latch, "temperature_c": self.source.temperature()})
        except Exception as e:  # noqa: BLE001 - no camera, no pypylon, permissions
            if self.source is not None:
                self.source.close()
            self.source = None
            log.warning("camera not opened: %s", e)
            if isinstance(e, ImportError):
                message = (f"pypylon is not installed for {sys.executable}: run bash scripts/install_pi.sh "
                           "--camera, or set [camera] python in config.toml to the camera environment")
            else:
                message = f"No camera: {e}"
            self.link.send({"ev": "error", "fatal": True, "where": "open", "message": message})

    def _measure_fps(self) -> float | None:
        stamps = []
        self.source.start(latest_only=False)
        try:
            deadline = time.monotonic() + 1.5
            while len(stamps) < 60 and time.monotonic() < deadline:
                item = self.source.grab(0.5)
                if item is not None:
                    stamps.append(item[1])
        finally:
            self.source.stop()
        return float(1e9 / np.median(np.diff(stamps))) if len(stamps) > 2 else None

    # ---------------------------------------------------------- main loops --
    def run(self) -> None:
        self.hello()
        self.open()
        grab = threading.Thread(target=self._grab_loop, name="camera-grab", daemon=True)
        grab.start()
        try:
            while not self.quit.is_set():
                try:
                    msg = self.link.recv()
                except (EOFError, OSError):
                    break
                try:
                    self._handle(msg)
                except Exception as e:  # noqa: BLE001 - keep serving
                    log.exception("command %s failed", msg.get("op"))
                    self.link.send({"ev": "error", "fatal": False, "message": f"{msg.get('op')}: {e}"})
        finally:
            rec = self.rec
            if rec is not None:
                rec.request_stop("the app closed")
                deadline = time.monotonic() + 5
                while self.rec is not None and time.monotonic() < deadline:
                    time.sleep(0.05)
            self.quit.set()
            grab.join(5)
            for t in self.finalizers:
                t.join(120)
            if self.source is not None:
                self.source.close()

    def _queue_op(self, fn) -> None:
        with self.ops_lock:
            self.ops.append(fn)

    def _run_ops(self) -> None:
        with self.ops_lock:
            ops, self.ops = self.ops, []
        for fn in ops:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log.exception("camera operation failed")
                self.link.send({"ev": "error", "fatal": False, "message": str(e)})

    def _grab_loop(self) -> None:
        mode = None
        last_preview = last_status = 0.0
        last_frame_at = time.monotonic()
        while True:
            self._run_ops()
            rec = self.rec
            if self.quit.is_set() and rec is None:
                break
            src = self.source
            if src is None:
                time.sleep(0.1)
                mode = None
                continue
            want = "record" if rec is not None else "preview"
            try:
                if want != mode:
                    src.start(latest_only=want == "preview")
                    mode = want
                    last_frame_at = time.monotonic()
                    if rec is not None:
                        rec.begin(src)
                item = src.grab(0.2)
            except Exception as e:  # noqa: BLE001 - camera unplugged, USB error
                self._camera_failed(e)
                continue
            host = now_ns()
            mono = time.monotonic()
            if item is not None:
                image, stamp, block = item
                self.last_image = image
                last_frame_at = mono
                if rec is not None:
                    rec.on_frame(image, stamp, block, host)
            if rec is not None:
                if item is None and mono - last_frame_at > NO_FRAMES_S:
                    rec.fail("the camera stopped sending frames")
                rec.periodic(src, mono)
                if rec.stop_request is not None:
                    self._end_recording(rec)
            if self.last_image is not None and mono - last_preview >= 1.0 / self.preview_hz:
                last_preview = mono
                self._send_preview()
            if mono - last_status >= 1.0:
                last_status = mono
                self._send_status()

    def _end_recording(self, rec: Recording) -> None:
        rec.close(self.source)
        self.rec = None
        t = threading.Thread(target=rec.finalize, name="recording-finish")
        t.start()
        self.finalizers = [f for f in self.finalizers if f.is_alive()] + [t]

    def _camera_failed(self, exc: BaseException) -> None:
        log.error("camera failed: %s", exc)
        self.link.send({"ev": "error", "fatal": True, "where": "camera", "message": f"Camera error: {exc}"})
        if self.rec is not None:
            self.rec.fail(f"camera error: {exc}")
            self._end_recording(self.rec)
        if self.source is not None:
            self.source.close()
        self.source = None

    # ------------------------------------------------------------- messages --
    def _send_preview(self) -> None:
        image = self.last_image
        h, w = image.shape[:2]
        if self.zoom:
            rows, cols = min(ZOOM_SIZE[0], h), min(ZOOM_SIZE[1], w)
            y0, x0 = (h - rows) // 2, (w - cols) // 2
            shown, scale = image[y0:y0 + rows, x0:x0 + cols], 1.0
        else:
            y0 = x0 = 0
            shown = shrink(image, self.preview_scale)
            scale = shown.shape[1] / w
        sample = image[::4, ::4]
        self.link.send({"ev": "preview", "shape": list(shown.shape), "zoom": self.zoom, "scale": scale,
                        "x0": x0, "y0": y0, "full": [h, w], "sat": float(np.mean(image >= 255) * 100),
                        "p999": float(np.percentile(sample, 99.9))},
                       np.ascontiguousarray(shown, dtype=np.uint8).tobytes())

    def _send_snapshot(self) -> None:
        image = self.last_image
        if image is None:
            self.link.send({"ev": "error", "fatal": False, "message": "No frame yet for a snapshot."})
            return
        self.link.send({"ev": "snapshot", "shape": list(image.shape)},
                       np.ascontiguousarray(image, dtype=np.uint8).tobytes())

    def _send_status(self) -> None:
        src, rec = self.source, self.rec
        now = time.monotonic()
        msg = {"ev": "status", "state": "recording" if rec else ("preview" if src else "no camera"),
               "fps": self.fps, "exposure_us": self.exposure, "zoom": self.zoom,
               "temperature_c": src.temperature() if src else None, "ram_available": memory_available(),
               "finishing": sum(1 for f in self.finalizers if f.is_alive())}
        if rec is not None:
            dt = max(now - self._last_status, 1e-3)
            rate = (rec.bytes_written - self._last_bytes) / dt
            self._last_bytes = rec.bytes_written
            frame_bytes = rec.width * rec.height
            incoming = self.fps * frame_bytes if self.fps else 0.0
            room = rec.queue.budget - rec.queue.bytes
            hold = room / (incoming - rate) if incoming > rate else None
            try:
                free = shutil.disk_usage(rec.folder).free
            except OSError:
                free = None
            msg.update({"rec_s": rec._elapsed(), "frames": rec.idx, "saved": len(rec.saved),
                        "matched": len(rec.matched), "queue": rec.queue.pending(), "dropped": len(rec.queue.dropped),
                        "skipped": len(rec.queue.skipped) + rec.skipped_low_disk, "missed": rec.missed_targets,
                        "high_speed": rec.high_speed, "write_mb_s": rate / 1e6, "hold_s": hold,
                        "disk_free": free, "low_disk": rec.low_disk, "windows": len(rec.windows),
                        "fallback": rec.no_samples()})
        else:
            self._last_bytes = 0
        self._last_status = now
        self.link.send(msg)

    def _handle(self, msg: dict) -> None:
        op = msg.get("op")
        rec = self.rec
        if op == "exposure":
            self._queue_op(lambda: self._set_exposure(msg))
        elif op == "zoom":
            self.zoom = bool(msg.get("on"))
        elif op == "preview_hz":
            self.preview_hz = max(1.0, float(msg.get("hz") or 10.0))
        elif op == "snapshot":
            self._queue_op(self._send_snapshot)
        elif op == "record_start":
            self._start_recording(msg)
        elif op == "targets":
            if rec is not None:
                rec.add_targets(msg.get("t") or [])
        elif op == "high_speed":
            if rec is not None:
                rec.request_high_speed(bool(msg.get("on")))
        elif op == "marker":
            if rec is not None:
                rec.mark(msg.get("label"))
        elif op == "record_stop":
            if rec is not None:
                rec.request_stop(msg.get("reason") or "stopped", bool(msg.get("dump")))
        elif op == "sim_state":
            if isinstance(self.source, SimSource):
                self.source.sim.set_state(float(msg.get("ext_mm") or 0.0), bool(msg.get("broken")))
        elif op == "reopen":
            self._queue_op(self._reopen)
        elif op == "shutdown":
            self.quit.set()
            if rec is not None:
                rec.request_stop("the camera was shut down")

    def _set_exposure(self, msg: dict) -> None:
        if self.source is None:
            return
        if self.rec is not None:
            # DIC compares every frame with frame 0, so the exposure stays as it was when the recording started.
            log.warning("exposure not changed: a recording is running")
            return
        target = float(msg["us"]) if msg.get("us") else self.exposure * float(msg.get("factor") or 1.0)
        self.exposure = self.source.set_exposure(target)
        self.fps = self.source.fps() or self.fps

    def _reopen(self) -> None:
        if self.source is not None:
            self.source.close()
            self.source = None
        self.open()

    def _start_recording(self, msg: dict) -> None:
        if self.source is None:
            self.link.send({"ev": "recording_failed", "reason": "no camera"})
            return
        if self.rec is not None:
            self.link.send({"ev": "recording_failed", "reason": "already recording"})
            return
        try:
            rec = Recording(self, msg)
        except Exception as e:  # noqa: BLE001
            log.exception("recording did not start")
            self.link.send({"ev": "recording_failed", "reason": str(e)})
            return
        self.rec = rec
        self.link.send({"ev": "recording", "folder": str(rec.folder)})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="VSBC camera process (started by the GUI)")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--camera-dir", required=True)
    p.add_argument("--sim", action="store_true", help="synthetic camera")
    p.add_argument("--sim-size", default="640x160", help="simulated frame size, WIDTHxHEIGHT")
    p.add_argument("--sim-fps", type=float, default=100.0)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    key = bytes.fromhex(os.environ.get(KEY_ENV, ""))
    conn = Client(("127.0.0.1", args.port), authkey=key)
    service = Service(Link(conn), args)
    try:
        service.run()
    finally:
        try:
            conn.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
