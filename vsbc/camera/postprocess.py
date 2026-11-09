"""The post-processing queue: the video, DIC, strain and crack tips after each
test, one job at a time in the camera environment (python -m vsbc.camera.jobs),
at low priority.

On Linux a running job is paused (SIGSTOP to its process group) while a test
records and resumed afterwards, so it never competes with the camera for the
CPU or the disk. Elsewhere it keeps running at below-normal priority.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from vsbc.camera.client import camera_env, camera_python
from vsbc.camera.jobs import STATUS_FILE, dic_selection
from vsbc.camera.roi import load_roi
from vsbc.camera.scripts import CRACK_IMAGE, CRACK_OPENING, DIC_RUN
from vsbc.camera.sync import read_json
from vsbc.config import REPO_ROOT, Config

log = logging.getLogger(__name__)

ANIMATION_FRAMES = 300


@dataclass
class Job:
    title: str
    kind: str                       # dic, strain, crack_image, crack_opening, annotate, video, package
    args: list[str]
    run: Path                       # the recording folder (status and log go there)
    needs: tuple[str, ...] = ()     # skipped when one of these failed
    result: str = ""
    ok: bool | None = None


@dataclass
class Options:
    """What to do after a test (the Camera panel's "After the test" group)."""

    run_dic: bool = True
    run_strain: bool = True
    strain_animation: bool = False
    crack_tip_image: bool = True
    crack_tip_opening: bool = True
    make_video: bool = True
    video_fps: float = 30.0
    overview_every: int = 0          # 0 = DIC_STEP / SAVE_EVERY from the script
    crack_every: int = 0
    subset_size: int = 0
    subset_step: int = 0
    max_displacement: int = 0
    redo: bool = False               # DIC again on frames that already have results
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Config) -> "Options":
        c = config.camera
        return cls(c.run_dic, c.run_strain, c.strain_animation, c.crack_tip_image, c.crack_tip_opening,
                   c.make_video, c.video_fps)


def _saved_count(run: Path, overview_every: int, crack_every: int) -> int:
    """How many frames the DIC job will analyze (for the animation step)."""
    import csv

    import numpy as np

    try:
        with open(run / "frames.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return 0
    saved = np.array(sorted(int(r["frame_index"]) for r in rows if r.get("saved") == "1"), dtype=np.int64)
    windows = read_json(run / "capture_settings.json").get("full_rate_windows") or []
    overview, window = dic_selection(saved, windows, max(1, overview_every), max(1, crack_every))
    return len(overview) + len(window)


class PostProcessor:
    def __init__(self, config: Config):
        self.config = config
        self.queue: deque[Job] = deque()
        self.done: list[Job] = []
        self.current: Job | None = None
        self.proc: subprocess.Popen | None = None
        self.paused = False
        self.text = ""
        self.progress: float | None = None
        self._log = None

    # -------------------------------------------------------------- queueing --
    def jobs_for(self, run: Path, test: Path | None, options: Options, camera_settings: dict) -> list[Job]:
        """The jobs for one recording, in order: video first (quick), then DIC and what needs it."""
        run = Path(run)
        cam_dir = str(self.config.camera_dir)
        py_job = ["-m", "vsbc.camera.jobs"]
        name = (test or run).name
        jobs: list[Job] = []
        images = (run / "images").is_dir() and not (run / "images_deleted.txt").is_file()
        if options.make_video and images:
            args = py_job + ["video", "--run", str(run), "--fps", str(options.video_fps)]
            if test is not None:
                args += ["--test", str(test)]
            jobs.append(Job(f"{name}: video", "video", args, run))
        roi = load_roi(run / "roi.npz")
        has_results = any((run / "pyvale_results").glob("dic_results_*"))
        s = camera_settings
        overview = options.overview_every or max(1, int(s.get("DIC_STEP", 27)) // max(1, int(s.get("SAVE_EVERY", 9))))
        crack = options.crack_every or int(s.get("CRACK_DIC_STEP", 1))
        if options.run_dic:
            if roi is None or roi.problems():
                options.notes.append("DIC skipped: no usable ROI (Camera tab > Edit ROI)")
            elif not images:
                options.notes.append("DIC skipped: the camera images are gone")
            else:
                args = py_job + ["dic", "--run", str(run), "--camera-dir", cam_dir,
                                 "--overview-every", str(overview), "--crack-every", str(crack)]
                for flag, value in (("--subset-size", options.subset_size), ("--subset-step", options.subset_step),
                                    ("--max-displacement", options.max_displacement)):
                    if value:
                        args += [flag, str(value)]
                if options.redo:
                    args.append("--redo")
                jobs.append(Job(f"{name}: DIC", "dic", args, run))
        dic_ok = any(j.kind == "dic" for j in jobs) or has_results
        needs = ("dic",) if any(j.kind == "dic" for j in jobs) else ()

        def script(kind: str, title: str, script_name: str, settings: dict) -> Job:
            args = py_job + ["script", "--run", str(run), "--camera-dir", cam_dir, "--name", script_name]
            for key, value in settings.items():
                args += ["--set", f"{key}={json.dumps(value)}"]
            return Job(f"{name}: {title}", kind, args, run, needs)

        if options.run_strain and dic_ok:
            n = _saved_count(run, overview, crack)
            jobs.append(script("strain", "strain + plots", DIC_RUN, {
                "image_folder": str(run / "images"), "output_folder": str(run / "pyvale_results"),
                "RUN_DIC": False, "MAKE_ANIMATION": bool(options.strain_animation),
                "ANIM_STEP": max(1, math.ceil(n / ANIMATION_FRAMES))}))
        notch = roi.notch if roi is not None else None
        trackers = []
        if (options.crack_tip_image or options.crack_tip_opening) and dic_ok and notch is None:
            options.notes.append("Crack tips skipped: set the notch tip (Camera tab > Edit ROI)")
        elif dic_ok and (options.crack_tip_image or options.crack_tip_opening):
            base ={"RUN_FOLDER": str(run), "NOTCH_TIP": [int(notch[0]), int(notch[1])],
                    "CRACK_FROM_LEFT": bool(roi.crack_from_left)}
            if options.crack_tip_image:
                trackers.append(script("crack_image", "crack tip (image)", CRACK_IMAGE, base))
            if options.crack_tip_opening:
                subset = options.subset_size or int(s.get("subset_size", 41))
                trackers.append(script("crack_opening", "crack tip (opening)", CRACK_OPENING,
                                       {**base, "subset_size": subset}))
        jobs += trackers
        if trackers:
            jobs.append(Job(f"{name}: crack-tip times and load", "annotate",
                            py_job + ["annotate", "--run", str(run)], run))
        if self.config.storage.upload_zips:
            # Last, and needing nothing: the zip is made even when DIC failed.
            jobs.append(Job(f"{name}: upload zip", "package",
                            py_job + ["package", "--folder", str(test or run),
                                      "--output-dir", str(self.config.output_dir)], run))
        return jobs

    def add(self, jobs: list[Job]) -> None:
        self.queue.extend(jobs)

    @property
    def busy(self) -> bool:
        return self.proc is not None or bool(self.queue)

    # --------------------------------------------------------------- running --
    def poll(self) -> None:
        """Advance the queue; call it a few times a second from the GUI."""
        if self.proc is not None:
            code = self.proc.poll()
            self._read_status()
            if code is None:
                return
            job = self.current
            job.ok = code == 0
            job.result = self.text or ("done" if job.ok else f"failed (exit code {code})")
            self.done.append(job)
            log.info("post-processing %s: %s", job.title, job.result)
            self.proc, self.current = None, None
            if self._log is not None:
                self._log.close()
                self._log = None
        if self.paused or not self.queue:
            return
        job = self.queue.popleft()
        failed = {j.kind for j in self.done if j.run == job.run and j.ok is False}
        if failed.intersection(job.needs):
            job.ok, job.result = False, f"skipped: {', '.join(failed.intersection(job.needs))} failed"
            self.done.append(job)
            return
        self._start(job)

    def _start(self, job: Job) -> None:
        (job.run / STATUS_FILE).unlink(missing_ok=True)
        self._log = open(job.run / "postprocess.log", "a", encoding="utf-8")
        self._log.write(f"\n==== {job.title}\n")
        self._log.flush()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True     # its own process group, for pause / resume
        try:
            self.proc = subprocess.Popen([camera_python(self.config)] + job.args, env=camera_env(),
                                         cwd=str(REPO_ROOT), stdout=self._log, stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            job.ok, job.result = False, f"could not start: {e}"
            self.done.append(job)
            self._log.close()
            self._log = None
            return
        self.current = job
        self.text, self.progress = f"{job.title}: starting", 0.0

    def _read_status(self) -> None:
        job = self.current
        if job is None:
            return
        try:
            data = json.loads((job.run / STATUS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.text = data.get("text") or self.text
        self.progress = data.get("progress")

    def pause(self) -> bool:
        """Pause the running job (Linux); True if it is paused."""
        self.paused = True
        if self.proc is not None and hasattr(os, "killpg"):
            try:
                os.killpg(self.proc.pid, signal.SIGSTOP)
                return True
            except OSError:
                return False
        return False

    def resume(self) -> None:
        self.paused = False
        if self.proc is not None and hasattr(os, "killpg"):
            try:
                os.killpg(self.proc.pid, signal.SIGCONT)
            except OSError:
                pass

    def running_for(self, run: Path) -> bool:
        """A job for this recording folder is running or queued."""
        run = Path(run)
        return (self.current is not None and Path(self.current.run) == run) or any(Path(j.run) == run
                                                                                   for j in self.queue)

    def cancel_run(self, run: Path) -> None:
        """Drop this recording's queued jobs and stop its running one; other tests' jobs carry on."""
        run = Path(run)
        self.queue = deque(j for j in self.queue if Path(j.run) != run)
        if self.current is not None and Path(self.current.run) == run:
            self._terminate()

    def cancel(self) -> None:
        self.queue.clear()
        self._terminate()

    def _terminate(self) -> None:
        proc = self.proc
        if proc is not None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGCONT)
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except OSError:
                pass

    def stop(self) -> None:
        """App closing: cancel and wait briefly."""
        self.cancel()
        if self.proc is not None:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def summary(self) -> str:
        if self.current is not None:
            pct = f" ({100 * self.progress:.0f} %)" if isinstance(self.progress, (int, float)) else ""
            paused = " [paused during the test]" if self.paused else ""
            return f"{self.text}{pct}{paused}. {len(self.queue)} more job(s) queued."
        if self.queue:
            return f"{len(self.queue)} job(s) queued" + (" [paused during the test]" if self.paused else "")
        if self.done:
            last = self.done[-1]
            return f"Last: {last.title}: {last.result}"
        return "Nothing to do."
