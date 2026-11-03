"""Post-processing jobs, run in the camera environment by the GUI's PostProcessor
(or `vsbc camera process`), one job per process:

    python -m vsbc.camera.jobs dic      --run RUN --camera-dir DIR [--overview-every N] [--crack-every M]
    python -m vsbc.camera.jobs script   --run RUN --camera-dir DIR --name pyvale_dic_run.py --set KEY=JSON ...
    python -m vsbc.camera.jobs annotate --run RUN
    python -m vsbc.camera.jobs video    --run RUN [--test TEST] [--fps 30]
    python -m vsbc.camera.jobs check    --camera-dir DIR --output-dir DIR [--sim]

RUN is a recording folder (a test's camera/ folder). Progress goes to
RUN/postprocess_status.json, which the GUI polls; printed output goes to
RUN/postprocess.log. Jobs run at low priority (nice 10), so they do not slow
down a test that starts meanwhile.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from vsbc.camera import tiff
from vsbc.camera.scripts import capture_settings, check_scripts, load_module, override_settings
from vsbc.camera.sync import annotate_crack_csv, in_windows, read_json, video_schedule, write_json
from vsbc.units import units_from_dict

STATUS_FILE = "postprocess_status.json"


def lower_priority() -> None:
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


class Status:
    """RUN/postprocess_status.json: {"job", "state", "text", "progress", "updated"}."""

    def __init__(self, run: Path, job: str):
        self.path = Path(run) / STATUS_FILE
        self.job = job

    def update(self, text: str, progress: float | None = None, state: str = "running") -> None:
        print(f"[{self.job}] {text}", flush=True)
        try:
            write_json(self.path, {"job": self.job, "state": state, "text": text, "progress": progress,
                                   "updated": time.time()})
        except OSError:
            pass

    def done(self, text: str) -> None:
        self.update(text, 1.0, "done")

    def error(self, text: str) -> None:
        self.update(text, None, "error")


def _frames(run: Path) -> tuple[list[str], list[list[str]]]:
    with open(run / "frames.csv", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, [r for r in reader if r]


def _saved_frames(run: Path) -> np.ndarray:
    header, rows = _frames(run)
    i, s = header.index("frame_index"), header.index("saved")
    return np.array(sorted(int(r[i]) for r in rows if r[s] == "1"), dtype=np.int64)


# ------------------------------------------------------------------- live DIC --

def dic_process_entry(camera_dir: str, cfg: dict, done_q, status_q) -> None:
    """Live DIC during a recording: the user's dic_worker in a process spawned by the camera process."""
    lower_priority()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    module = load_module(Path(camera_dir))
    module.dic_worker(cfg, done_q, status_q)


# ------------------------------------------------------------------------ DIC --

def dic_selection(saved: np.ndarray, windows, overview_every: int, crack_every: int) -> tuple[list[int], list[int]]:
    """(overview frames, full-rate window frames) to run DIC on; frame 0 is the reference."""
    held = in_windows(saved, windows)
    routine = [int(i) for i in saved[~held] if i != 0]
    window = [int(i) for i in saved[held] if i != 0]
    return routine[::max(1, overview_every)], window[::max(1, crack_every)]


def job_dic(args) -> None:
    run = Path(args.run)
    status = Status(run, "dic")
    if (run / "images_deleted.txt").is_file():
        raise RuntimeError("the camera images of this recording were deleted, so DIC cannot run")
    if not (run / "roi.npz").is_file():
        raise RuntimeError("no roi.npz: draw the ROI (Camera tab > Edit ROI) and process the folder again")
    module = load_module(Path(args.camera_dir))
    if not hasattr(module, "dic_worker"):
        raise RuntimeError("capture_dic_live.py has no dic_worker()")
    s = capture_settings(module)
    settings = read_json(run / "capture_settings.json")
    overview, window = dic_selection(_saved_frames(run), settings.get("full_rate_windows") or [],
                                     args.overview_every or max(1, int(s["DIC_STEP"]) // max(1, int(s["SAVE_EVERY"]))),
                                     args.crack_every or int(s["CRACK_DIC_STEP"]))
    out_dir = run / "pyvale_results"
    out_dir.mkdir(exist_ok=True)
    todo = [i for i in sorted(set(overview) | set(window))
            if args.redo or not (out_dir / f"dic_results_frame_{i:06d}.csv").is_file()]
    if not todo:
        status.done("DIC: every selected frame already has results")
        return
    roi = np.load(run / "roi.npz")
    cfg = dict(frames_dir=str(run / "images"), out_dir=str(out_dir), step=1,
               roi_mask=roi["mask"].astype(bool), roi_seed=[int(v) for v in roi["seed"]],
               subset_size=args.subset_size or int(s["subset_size"]),
               subset_step=args.subset_step or int(s["subset_step"]),
               max_displacement=args.max_displacement or int(s["max_displacement"]),
               threads=args.threads or int(s["DIC_THREADS"]), fill_batch=int(s["FILL_BATCH"]), window=False)
    done_q, status_q = queue.Queue(), queue.Queue()
    for i in [0] + todo:
        done_q.put(i)
    if window:
        done_q.put(("crack", window))
    done_q.put(None)
    total = len(todo)
    errors: list[str] = []
    status.update(f"DIC on {total} frames ({len(overview)} overview, {len(window)} full-rate)", 0.0)
    stop = threading.Event()

    def relay():
        while not stop.is_set() or not status_q.empty():
            try:
                msg = status_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg[0] == "fill":
                status.update(f"DIC: {msg[1]} of {total} frames left", 1.0 - msg[1] / max(total, 1))
            elif msg[0] == "error":
                errors.append(str(msg[1]))
                print(f"DIC error: {msg[1]}", flush=True)

    relay_thread = threading.Thread(target=relay, daemon=True)
    relay_thread.start()
    t0 = time.perf_counter()
    try:
        module.dic_worker(cfg, done_q, status_q)
    finally:
        stop.set()
        relay_thread.join(5)
    per_frame = (time.perf_counter() - t0) / max(total, 1)
    text = f"DIC done: {total} frames, {per_frame:.2f} s per frame"
    if errors:
        text += f"; {len(errors)} error(s), first: {errors[0]}"
    status.done(text)


# --------------------------------------------------------------------- scripts --

def run_script(path: Path, overrides: dict, run: Path, camera_dir: Path) -> list[Path]:
    """Run a camera script as __main__ with its settings overridden; each plt.show() saves the figures."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    code = override_settings(Path(path).read_text(encoding="utf-8"), overrides)
    figdir = Path(run) / "plots"
    figdir.mkdir(parents=True, exist_ok=True)
    stem = Path(path).stem
    saved: list[Path] = []

    def save_figures(*_args, **_kwargs):
        for num in plt.get_fignums():
            target = figdir / f"{stem}_{len(saved) + 1:02d}.png"
            plt.figure(num).savefig(target, dpi=110)
            saved.append(target)
        plt.close("all")

    old_show, plt.show = plt.show, save_figures
    old_argv, old_cwd = sys.argv, os.getcwd()
    sys.argv = [str(path)]
    if str(camera_dir) not in sys.path:
        sys.path.insert(0, str(camera_dir))
    os.chdir(run)
    try:
        namespace = {"__name__": "__main__", "__file__": str(path), "__builtins__": __builtins__}
        exec(compile(code, str(path), "exec"), namespace)
        save_figures()       # figures the script made but never showed
    finally:
        plt.show = old_show
        sys.argv = old_argv
        os.chdir(old_cwd)
    return saved


def job_script(args) -> None:
    run, camera_dir = Path(args.run), Path(args.camera_dir)
    status = Status(run, Path(args.name).stem)
    overrides = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        overrides[key] = json.loads(value)
    print(f"{args.name} with {overrides}", flush=True)
    status.update(f"Running {args.name}", None)
    figures = run_script(camera_dir / args.name, overrides, run, camera_dir)
    status.done(f"{args.name} done ({len(figures)} figure(s) in plots/)")


def job_annotate(args) -> None:
    run = Path(args.run)
    status = Status(run, "annotate")
    done = [name for name in ("crack_tip_image.csv", "crack_tip_opening.csv") if annotate_crack_csv(run / name, run)]
    status.done(f"Added test time and load to {', '.join(done) or 'no crack-tip files'}")


# ----------------------------------------------------------------------- video --

def _saved_table(run: Path) -> dict[str, np.ndarray]:
    header, rows = _frames(run)
    if "time_s" not in header:
        raise RuntimeError("frames.csv has no time_s column: the recording was not synced")
    col = {name: header.index(name) for name in header}
    saved = [r for r in rows if r[col["saved"]] == "1" and r[col["time_s"]] != ""]

    def floats(name):
        if name not in col:
            return np.full(len(saved), np.nan)
        return np.array([float(r[col[name]]) if r[col[name]] != "" else np.nan for r in saved])

    return {"index": np.array([int(r[col["frame_index"]]) for r in saved], dtype=np.int64),
            "time": floats("time_s"), "load": floats("load_N"), "ext": floats("extension_mm")}


class _VideoWriter:
    """H.264 through the ffmpeg program when it is installed, else OpenCV's MPEG-4."""

    def __init__(self, path: Path, width: int, height: int, fps: float):
        self.ffmpeg = shutil.which("ffmpeg")
        self.size = (width, height)
        if self.ffmpeg:
            self.proc = subprocess.Popen(
                [self.ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{width}x{height}", "-r", f"{fps:.6f}", "-i", "-",
                 "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
                stdin=subprocess.PIPE)
        else:
            import cv2

            self.writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
            if not self.writer.isOpened():
                raise RuntimeError("OpenCV could not open an MP4 writer; install ffmpeg (sudo apt install ffmpeg)")

    def write(self, bgr: np.ndarray) -> None:
        if self.ffmpeg:
            self.proc.stdin.write(np.ascontiguousarray(bgr).tobytes())
        else:
            self.writer.write(bgr)

    def close(self) -> None:
        if self.ffmpeg:
            self.proc.stdin.close()
            if self.proc.wait() != 0:
                raise RuntimeError("ffmpeg could not write the video")
        else:
            self.writer.release()


def job_video(args) -> None:
    import cv2

    run = Path(args.run)
    test = Path(args.test) if args.test else None
    status = Status(run, "video")
    if (run / "images_deleted.txt").is_file():
        raise RuntimeError("the camera images of this recording were deleted")
    meta = read_json(test / "meta.json") if test else {}
    settings = read_json(run / "capture_settings.json")
    units = units_from_dict(meta.get("units") or settings.get("units"))
    table = _saved_table(run)
    order = np.argsort(table["time"])
    for key in table:
        table[key] = table[key][order]
    load_rate = float(settings.get("load_rate_hz") or 0.0)
    if test and (test / "data.csv").is_file():
        with open(test / "data.csv", newline="", encoding="utf-8") as f:
            data_t = np.array([float(r["time_s"]) for r in csv.DictReader(f) if r.get("time_s")])
        if data_t.size > 2:
            load_rate = 1.0 / float(np.median(np.diff(data_t)))
        t_start, t_end = 0.0, float(data_t[-1]) if data_t.size else float(table["time"][-1])
    else:
        t_start, t_end = float(table["time"][0]), float(table["time"][-1])
    fps = min(args.fps, load_rate) if load_rate > 0 else args.fps
    times, pick = video_schedule(table["time"], fps, t_end, t_start)
    if pick.size == 0:
        raise RuntimeError("no saved frames to make a video from")
    out = (test or run) / "video.mp4"
    first = tiff.read(run / "images" / f"frame_{int(table['index'][pick[0]]):06d}.tif")
    height, width = first.shape
    writer = _VideoWriter(out, width, height, fps)
    scale = max(0.45, width / 1600.0)
    thickness = max(1, int(round(scale * 2)))
    band = int(34 * scale)
    cache_idx, cache = -1, None
    status.update(f"Video: {times.size} frames at {fps:.2f} fps", 0.0)
    try:
        for k, j in enumerate(pick):
            idx = int(table["index"][j])
            if idx != cache_idx:
                cache = tiff.read(run / "images" / f"frame_{idx:06d}.tif")
                cache_idx = idx
            bgr = cv2.cvtColor(cache, cv2.COLOR_GRAY2BGR)
            load, ext = table["load"][j], table["ext"][j]
            text = f"t = {table['time'][j]:.3f} s"
            if math.isfinite(load):
                text += f"   F = {units.force_from_n(load):.{units.force_digits}f} {units.force}"
            if math.isfinite(ext):
                text += f"   ext = {units.length_from_mm(ext):.{units.length_digits - 1}f} {units.length}"
            text += f"   frame {idx}"
            cv2.rectangle(bgr, (0, 0), (width, band), (0, 0, 0), -1)
            cv2.putText(bgr, text, (int(8 * scale), int(band * 0.72)), cv2.FONT_HERSHEY_SIMPLEX, 0.8 * scale,
                        (255, 255, 255), thickness, cv2.LINE_AA)
            writer.write(bgr)
            if k % 50 == 0:
                status.update(f"Video: {k} of {times.size} frames", k / times.size)
    finally:
        writer.close()
    status.done(f"Video: {out.name}, {times.size} frames at {fps:.2f} fps (playback time = data.csv time_s)")


# ----------------------------------------------------------------------- check --

def _check_dic(report: dict, module) -> None:
    """A tiny synthetic DIC: two speckle images 3 px / 2 px apart, through the same call dic_worker makes."""
    import pyvale.dic as dic

    from vsbc.camera.simcam import SimCamera

    pattern = SimCamera(256, 256, seed=3)._render()
    shifted = np.roll(pattern, (2, 3), axis=(0, 1))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiff.write_gray8(tmp / "frame_000000.tif", pattern)
        tiff.write_gray8(tmp / "frame_000001.tif", shifted)
        mask = np.zeros(pattern.shape, dtype=bool)
        mask[60:196, 60:196] = True
        s = capture_settings(module)
        t0 = time.perf_counter()
        dic.calculate_2d(reference=str(tmp / "frame_000000.tif"), deformed=[tmp / "frame_000001.tif"],
                         roi_mask=mask, seed=[128, 128], subset_size=int(s["subset_size"]),
                         subset_step=int(s["subset_step"]), max_displacement=int(s["max_displacement"]),
                         num_threads=int(s["DIC_THREADS"]), output_basepath=str(tmp) + os.sep, print_level=0)
        seconds = time.perf_counter() - t0
        res = dic.import_2d(data=str(tmp / "dic_results_*"), delimiter=",", binary=False)
        conv = np.asarray(res.converged).astype(bool)
        u = float(np.nanmean(np.where(conv, np.asarray(res.u_px, float), np.nan)))
        v = float(np.nanmean(np.where(conv, np.asarray(res.v_px, float), np.nan)))
    ok = abs(u - 3) < 0.2 and abs(v - 2) < 0.2
    report["dic"] = {"ok": ok, "u_px": u, "v_px": v, "expected": [3, 2], "seconds": seconds}


def _check_camera(report: dict, module, sim: bool) -> tuple[int, int]:
    from vsbc.camera.service import PylonSource, SimSource

    source = SimSource((640, 160), 100.0) if sim else PylonSource(module)
    try:
        s = capture_settings(module)
        width, height = source.configure(float(s["EXPOSURE_US"]), s["CROP"], s["FPS"])
        source.start(latest_only=False)
        stamps, blocks = [], []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            item = source.grab(0.5)
            if item is not None:
                stamps.append(item[1])
                blocks.append(item[2])
        source.stop()
        fps = float(1e9 / np.median(np.diff(stamps))) if len(stamps) > 2 else None
        dropped = int(np.sum(np.diff(blocks) - 1)) if len(blocks) > 1 and min(blocks) >= 0 else None
        report["camera"] = {"ok": bool(stamps) and dropped in (0, None), "model": source.model,
                            "serial": source.serial, "width": width, "height": height, "frames": len(stamps),
                            "fps": fps, "dropped_frames": dropped, "grab_errors": source.grab_errors,
                            "timestamp_latch": source.has_latch}
        return width, height
    finally:
        source.close()


def _check_disk(report: dict, output_dir: Path, width: int, height: int, n: int = 150) -> None:
    folder = Path(output_dir) / ".vsbc_write_test"
    folder.mkdir(parents=True, exist_ok=True)
    frame = np.random.default_rng(0).integers(0, 255, (height, width), dtype=np.uint8)
    try:
        t0 = time.perf_counter()
        for i in range(n):
            tiff.write_gray8(folder / f"frame_{i:06d}.tif", frame)
        if hasattr(os, "sync"):
            os.sync()
        seconds = time.perf_counter() - t0
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    mb_s = n * frame.nbytes / 1e6 / seconds
    report["disk"] = {"ok": True, "write_mb_s": mb_s, "frames_per_s": n / seconds,
                      "free_gb": shutil.disk_usage(output_dir).free / 1024 ** 3}


def format_check(report: dict) -> str:
    """The check's report, readable (for the GUI and `vsbc camera check`)."""
    if not report:
        return "The check did not produce a report (see Outputs/logs/camera.log)."
    lines = ["Versions: " + ", ".join(f"{k} {v}" for k, v in (report.get("versions") or {}).items())]
    lines += report.get("scripts") or []
    cam = report.get("camera") or {}
    if cam.get("error"):
        lines.append(f"Camera: ✗ {cam['error']}")
    elif cam:
        lines.append(f"Camera: {'✓' if cam.get('ok') else '✗'} {cam.get('model')} {cam.get('width')}×{cam.get('height')}, "
                     f"{cam.get('frames')} frames in 3 s at {cam.get('fps') or 0:.0f} fps, "
                     f"{cam.get('dropped_frames')} dropped, timestamp latch {'yes' if cam.get('timestamp_latch') else 'no'}")
    disk = report.get("disk") or {}
    if disk.get("error"):
        lines.append(f"Disk: ✗ {disk['error']}")
    elif disk:
        lines.append(f"Disk: writes {disk.get('write_mb_s', 0):.0f} MB/s ({disk.get('frames_per_s', 0):.0f} frames/s), "
                     f"{disk.get('free_gb', 0):.1f} GB free")
    dic = report.get("dic") or {}
    if dic.get("error"):
        lines.append(f"pyvale DIC: ✗ {dic['error']}")
    elif dic:
        lines.append(f"pyvale DIC: {'✓' if dic.get('ok') else '✗'} measured u = {dic.get('u_px', 0):.2f}, "
                     f"v = {dic.get('v_px', 0):.2f} px (expected 3, 2) in {dic.get('seconds', 0):.1f} s")
    return "\n".join(lines)


def job_check(args) -> dict:
    report: dict = {"versions": {"python": sys.version.split()[0], "numpy": np.__version__}}
    for name in ("cv2", "pypylon", "pyvale"):
        try:
            report["versions"][name] = getattr(__import__(name), "__version__", "installed")
        except Exception as e:  # noqa: BLE001
            report["versions"][name] = f"missing ({e})"
    report["scripts"] = [c.text() for c in check_scripts(Path(args.camera_dir))]
    try:
        module = load_module(Path(args.camera_dir))
    except Exception as e:  # noqa: BLE001
        module = None
        report["scripts"].append(f"capture_dic_live.py does not load here: {e}")
    size = [1920, 400]

    def camera():
        size[:] = _check_camera(report, module, args.sim)

    checks = (("camera", camera), ("disk", lambda: _check_disk(report, Path(args.output_dir), *size)),
              ("dic", lambda: _check_dic(report, module)))
    for name, check in checks:        # each one reports, whatever the others did
        try:
            check()
        except Exception as e:  # noqa: BLE001
            report[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = Path(args.output_dir) / "camera_check.json"
    write_json(out, report)
    print(format_check(report), flush=True)
    return report


# ------------------------------------------------------------------------ main --

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vsbc.camera.jobs")
    sub = p.add_subparsers(dest="job", required=True)
    d = sub.add_parser("dic")
    d.add_argument("--run", required=True)
    d.add_argument("--camera-dir", required=True)
    d.add_argument("--overview-every", type=int, default=0, help="DIC on every Nth saved frame (0 = from the script)")
    d.add_argument("--crack-every", type=int, default=0)
    d.add_argument("--subset-size", type=int, default=0)
    d.add_argument("--subset-step", type=int, default=0)
    d.add_argument("--max-displacement", type=int, default=0)
    d.add_argument("--threads", type=int, default=0)
    d.add_argument("--redo", action="store_true", help="also frames that already have results")
    s = sub.add_parser("script")
    s.add_argument("--run", required=True)
    s.add_argument("--camera-dir", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--set", action="append", help="KEY=JSON value")
    a = sub.add_parser("annotate")
    a.add_argument("--run", required=True)
    v = sub.add_parser("video")
    v.add_argument("--run", required=True)
    v.add_argument("--test")
    v.add_argument("--fps", type=float, default=30.0)
    c = sub.add_parser("check")
    c.add_argument("--camera-dir", required=True)
    c.add_argument("--output-dir", required=True)
    c.add_argument("--sim", action="store_true")
    args = p.parse_args(argv)

    lower_priority()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    jobs = {"dic": job_dic, "script": job_script, "annotate": job_annotate, "video": job_video, "check": job_check}
    run = Path(args.run) if getattr(args, "run", None) else None
    try:
        jobs[args.job](args)
    except Exception as e:  # noqa: BLE001 - report to the GUI, then fail
        traceback.print_exc()
        if run is not None:
            Status(run, args.job).error(f"{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
