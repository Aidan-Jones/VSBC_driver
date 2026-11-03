"""GUI side of the camera: starts the camera process, talks to it, and ties
recordings to tests.

The camera process (vsbc.camera.service) runs with the camera environment's
Python; this side never imports cv2, pypylon or pyvale. Besides relaying
commands it:

* logs every load sample's Mega time and arrival time while recording
  (camera/samples.csv, for the clock sync), and in "match" mode sends each
  sample's Pi time to the camera process, which saves the frame nearest it;
* gives PullTestRunner its before_run / after_run hooks (settle, start
  recording, wait for the reference frame; stop together with the trial);
* stops the trial when the recording stops by itself (disk full, camera
  lost, camera process gone), so the trial and the camera always stop together.

Events from the camera process reach `on_event` on the receiver thread; the
GUI posts them to the Qt thread. The latest preview frame is kept in
`preview` instead, so a busy GUI never queues up frames.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Callable

import numpy as np

from vsbc.camera import tiff
from vsbc.camera.roi import Roi, save_roi
from vsbc.config import REPO_ROOT, Config
from vsbc.protocol import Sample
from vsbc.recorder import safe_name

log = logging.getLogger(__name__)

KEY_ENV = "VSBC_CAMERA_KEY"
CONNECT_TIMEOUT_S = 30.0          # importing pypylon / opening the camera can take a while on the Pi
REFERENCE_TIMEOUT_S = 10.0
TARGET_SEND_S = 0.05
OFFSET_WINDOW_NS = 2_000_000_000


def camera_python(config: Config) -> str:
    """The camera environment's Python: [camera] python, else .venv-camera, else this app's."""
    if config.camera.python:
        return str(Path(config.camera.python).expanduser())
    venv = REPO_ROOT / ".venv-camera"
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(exe) if exe.is_file() else sys.executable


def camera_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")    # pyvale imports Qt; the camera side has no windows
    env.setdefault("MPLBACKEND", "Agg")
    return env


class CameraClient:
    def __init__(self, config: Config, machine, on_event: Callable[[dict], None] | None = None, sim: bool = False):
        self.config = config
        self.machine = machine
        self.on_event = on_event or (lambda msg: None)
        self.sim = sim or config.camera.simulate
        self.state = "off"                # off, starting, preview, recording, finishing, error
        self.error: str | None = None
        self.hello: dict = {}             # the camera process's settings, versions and script status
        self.info: dict = {}              # the camera: model, size, fps, exposure
        self.status: dict = {}
        self.preview: tuple[dict, np.ndarray] | None = None
        self.preview_seq = 0
        self.last_summary: dict | None = None
        self.runner = None                # the PullTestRunner while a test records
        self.recording: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._conn = None
        self._log = None
        self._send_lock = threading.Lock()
        self._samples: list[tuple] = []
        self._samples_lock = threading.Lock()
        self._targets: list[int] = []
        self._offsets: collections.deque = collections.deque()
        self._reference = threading.Event()
        self._finished = threading.Event()
        self._finished.set()
        self._start_error: str | None = None
        self._sim_peak = 0.0
        self._stop_sender = threading.Event()
        self._sender_thread: threading.Thread | None = None
        # Each start() is a new generation: threads of an older camera process
        # (still finishing after a restart) must not clean up the new one.
        self._generation = 0
        self._life_lock = threading.RLock()
        machine.add_listener(self._on_machine_message)

    # --------------------------------------------------------------- process --
    @property
    def running(self) -> bool:
        return self._conn is not None

    @property
    def log_path(self) -> Path:
        return self.config.logs_dir / "camera.log"

    def start(self) -> None:
        """Start the camera process (returns at once; "hello" / "opened" / "error" events follow)."""
        with self._life_lock:
            if self.state not in ("off", "error") or self._proc is not None and self._proc.poll() is None:
                return
            self._generation += 1
            self._start(self._generation)

    def _start(self, gen: int) -> None:
        self.state, self.error = "starting", None
        key = os.urandom(16)
        listener = Listener(("127.0.0.1", 0), authkey=key)
        port = listener.address[1]
        args = [camera_python(self.config), "-u", "-m", "vsbc.camera.service", "--port", str(port),
                "--camera-dir", str(self.config.camera_dir)]
        if self.sim:
            args.append("--sim")
        env = camera_env()
        env[KEY_ENV] = key.hex()
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        if self._log is not None:             # left open by a start that failed
            self._log.close()
        self._log = open(self.log_path, "a", encoding="utf-8")
        self._log.write(f"\n==== {datetime.now():%Y-%m-%d %H:%M:%S} camera process: {' '.join(args)}\n")
        self._log.flush()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(args, env=env, stdout=self._log, stderr=subprocess.STDOUT,
                                          cwd=str(REPO_ROOT), creationflags=flags)
        except OSError as e:
            listener.close()
            self._fail(f"cannot start the camera process with {args[0]}: {e}")
            return
        threading.Thread(target=self._accept, args=(listener, gen), name="camera-accept", daemon=True).start()
        self._stop_sender.clear()
        if self._sender_thread is None or not self._sender_thread.is_alive():   # still running after a crash
            self._sender_thread = threading.Thread(target=self._sender, name="camera-sender", daemon=True)
            self._sender_thread.start()

    def _accept(self, listener: Listener, gen: int) -> None:
        sock = getattr(getattr(listener, "_listener", None), "_socket", None)
        if sock is not None:
            sock.settimeout(0.5)
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        conn = None
        try:
            while conn is None:
                if gen != self._generation:
                    return
                try:
                    conn = listener.accept()
                except (socket.timeout, TimeoutError):
                    if self._proc is None or self._proc.poll() is not None or time.monotonic() > deadline:
                        self._fail(f"the camera process did not start (see {self.log_path})", gen)
                        return
                except (OSError, EOFError) as e:
                    self._fail(f"the camera process did not connect: {e}", gen)
                    return
        finally:
            listener.close()
        with self._life_lock:
            if gen != self._generation:
                conn.close()
                return
            self._conn = conn
        self._receive(conn, gen)

    def _receive(self, conn, gen: int) -> None:
        while True:
            try:
                msg = json.loads(conn.recv_bytes().decode("utf-8"))
                payload = conn.recv_bytes() if "nbytes" in msg else None
            except (EOFError, OSError, ValueError):
                break
            try:
                self._dispatch(msg, payload)
            except Exception:  # noqa: BLE001 - one bad message must not end the connection
                log.exception("camera event %s failed", msg.get("ev"))
        self._lost(gen)

    def stop(self, timeout: float = 8.0) -> None:
        """Shut the camera process down (it finishes writing a recording first)."""
        self._stop_sender.set()
        sender = self._sender_thread
        if sender is not None and sender is not threading.current_thread():
            sender.join(2 * TARGET_SEND_S + 1.0)
        proc = self._proc
        if proc is None:
            return
        self.send("shutdown")
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self._life_lock:
            if proc is self._proc:            # _lost() may have cleaned up already
                self._close()
            self.state = "off"

    def restart(self) -> None:
        self.stop()
        self.start()

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def _fail(self, message: str, gen: int | None = None) -> None:
        if gen is not None and gen != self._generation:
            return
        log.warning("camera: %s", message)
        self.error = message
        self.state = "error"
        self.on_event({"ev": "error", "fatal": True, "where": "start", "message": message})

    def _lost(self, gen: int) -> None:
        with self._life_lock:
            if gen != self._generation:       # a newer camera process started meanwhile: not ours to clean up
                return
            expected = self._stop_sender.is_set()
            proc = self._proc
            self._close()
            if self.recording is not None:
                self._recording_stopped_itself("the camera process stopped")
                self._finished.set()
            self._reference.set()
            self.state = "off" if expected else "error"
            if not expected:
                code = proc.poll() if proc is not None else None
                self.error = f"the camera process stopped (exit code {code}; see {self.log_path})"
        self.on_event({"ev": "lost", "expected": expected, "message": self.error})

    def send(self, op: str, **fields) -> bool:
        conn = self._conn
        if conn is None:
            return False
        data = json.dumps({"op": op, **fields}).encode("utf-8")
        with self._send_lock:
            try:
                conn.send_bytes(data)
                return True
            except (OSError, ValueError):
                return False

    # ---------------------------------------------------------------- events --
    def _dispatch(self, msg: dict, payload: bytes | None) -> None:
        ev = msg.get("ev")
        if ev in ("preview", "snapshot"):
            image = np.frombuffer(payload, dtype=np.uint8).reshape(msg["shape"])
            if ev == "preview":
                self.preview = (msg, image)
                self.preview_seq += 1
                return
            msg = {**msg, "image": image}
        elif ev == "hello":
            self.hello = msg
        elif ev == "opened":
            self.info = msg
            self.error = None
            self.state = "recording" if self.recording else "preview"
        elif ev == "error":
            if msg.get("fatal"):
                self.error = msg.get("message")
                if msg.get("where") in ("open", "camera"):
                    self.state = "error"
        elif ev == "status":
            self.status = msg
        elif ev == "recording":
            self.state = "recording"
        elif ev == "reference_saved":
            self._reference.set()
        elif ev == "recording_failed":
            self._start_error = msg.get("reason") or "unknown"
            self._reference.set()
        elif ev == "stopped_itself":
            self._recording_stopped_itself(msg.get("reason") or "unknown")
        elif ev == "finishing":
            self.state = "finishing"
        elif ev == "finished":
            self.last_summary = msg.get("summary") or {}
            self._write_samples()
            self.recording = None
            self.state = "error" if self.error and not self.info else "preview"
            self._finished.set()
        self.on_event(msg)

    def _recording_stopped_itself(self, reason: str) -> None:
        """The trial and the camera stop together: stop the test that is recording."""
        runner = self.runner
        if runner is not None and self.recording and self.recording.get("test"):
            runner.request_stop(f"the camera stopped recording ({reason})")

    # --------------------------------------------------------- load samples --
    def _on_machine_message(self, msg) -> None:
        """Serial reader thread: every load sample while recording."""
        if not isinstance(msg, Sample):
            return
        rec = self.recording
        if rec is None:
            return
        host = time.perf_counter_ns()
        with self._samples_lock:
            self._samples.append((msg.t_ms, host, msg.pos_mm, msg.load_n, msg.state))
            if rec["mode"] == "match":
                sent = round(msg.t_ms * 1_000_000)
                value = host - sent
                offsets = self._offsets
                while offsets and offsets[-1][1] >= value:
                    offsets.pop()
                offsets.append((host, value))
                while offsets[0][0] < host - OFFSET_WINDOW_NS:
                    offsets.popleft()
                self._targets.append(int(sent + offsets[0][1] - rec["load_delay_ns"]))
        if self.sim:
            if msg.load_n == msg.load_n:          # not nan
                self._sim_peak = max(self._sim_peak, msg.load_n)
            rec["sim_state"] = (msg.pos_mm - rec.setdefault("sim_start_mm", msg.pos_mm),
                                self._sim_peak > 20 and msg.load_n < 0.5 * self._sim_peak)

    def _sender(self) -> None:
        last_sim = 0.0
        while not self._stop_sender.wait(TARGET_SEND_S):
            with self._samples_lock:
                targets, self._targets = self._targets, []
            if targets:
                self.send("targets", t=targets)
            rec = self.recording
            if self.sim and rec and rec.get("sim_state") and time.monotonic() - last_sim > 0.05:
                last_sim = time.monotonic()
                ext, broken = rec["sim_state"]
                self.send("sim_state", ext_mm=ext, broken=bool(broken))

    def _write_samples(self) -> None:
        rec = self.recording
        if rec is None:
            return
        with self._samples_lock:
            samples, self._samples = self._samples, []
        try:
            with open(Path(rec["folder"]) / "samples.csv", "w", newline="", encoding="utf-8") as f:
                f.write("t_ms,host_ns,position_mm,load_N,state\n")
                for t_ms, host, pos, load, state in samples:
                    f.write(f"{t_ms:.3f},{host},{pos:.4f},{load:.3f},{state}\n")
        except OSError as e:
            log.warning("could not write samples.csv: %s", e)

    # ------------------------------------------------------------ recording --
    def begin_recording(self, folder: Path, options: dict, roi: Roi | None, snapshot: np.ndarray | None,
                        test: bool) -> None:
        """Start recording into `folder` and wait for the reference frame (blocks up to 10 s)."""
        if self.state != "preview":
            raise RuntimeError(f"the camera is not ready ({self.error or self.state})")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        if roi is not None:
            save_roi(folder / "roi.npz", roi)
        if snapshot is not None:
            tiff.write_gray8(folder / "roi_snapshot.tif", snapshot)
        self._reference.clear()
        self._finished.clear()
        self._start_error = None
        self._sim_peak = 0.0
        with self._samples_lock:
            self._samples, self._targets = [], []
            self._offsets.clear()
        self.recording = {"folder": folder, "test": test, "mode": options.get("mode", "match"),
                          "load_delay_ns": float(options.get("load_delay_ms") or 0.0) * 1e6}
        msg = {k: v for k, v in options.items() if k not in ("load_delay_ms", "settle_s", "dump_on_break")}
        msg.update(folder=str(folder), test=test, load_rate_hz=getattr(self.machine, "sample_rate_hz", 0.0))
        if msg.get("live_dic"):
            if roi is None or roi.problems():
                msg["live_dic"] = None            # live DIC needs the ROI
            else:
                msg["live_dic"] = {**msg["live_dic"], "roi": str(folder / "roi.npz")}
        if not self.send("record_start", **msg):
            self.recording = None
            raise RuntimeError("the camera process is not running")
        if not self._reference.wait(REFERENCE_TIMEOUT_S) or self._start_error:
            error = self._start_error or f"no reference frame within {REFERENCE_TIMEOUT_S:g} s"
            self.send("record_stop", reason="did not start")
            self.recording = None
            self._finished.set()
            raise RuntimeError(f"the camera recording did not start: {error}")

    def stop_recording(self, reason: str, dump: bool = False) -> None:
        if self.recording is not None:
            self.send("record_stop", reason=reason, dump=dump)

    def wait_finished(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def test_hooks(self, options: dict, roi: Roi | None, snapshot: np.ndarray | None,
                   on_settle: Callable[[float], None] | None = None):
        """(before_run, after_run) for PullTestRunner."""
        settle = float(options.get("settle_s") or 0.0)

        def before_run(folder: Path, stop_event: threading.Event) -> None:
            if on_settle is not None:
                on_settle(settle)
            if settle > 0 and stop_event.wait(settle):
                raise InterruptedError("stopped during the settle time")
            self.begin_recording(Path(folder) / "camera", options, roi, snapshot, test=True)

        def after_run(move_end) -> None:
            reason = move_end.fields.get("reason", "?") if move_end is not None else "ERROR"
            self.stop_recording(reason, dump=reason == "BREAK" and bool(options.get("dump_on_break", True)))

        return before_run, after_run

    def manual_folder(self, name: str) -> Path:
        now = datetime.now()
        base = self.config.camera_recordings_dir / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}_{safe_name(name or 'recording')}"
        folder, n = base, 2
        while folder.exists():
            folder = base.with_name(f"{base.name}_{n}")
            n += 1
        return folder
