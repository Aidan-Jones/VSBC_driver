# -*- coding: utf-8 -*-
"""
capture_dic_live.py

Basler a2A1920-160umBAS test capture + Pyvale DIC (Raspberry Pi 5 or laptop).

Stages
  1. PREVIEW  - live view to adjust before anything is recorded
                 +/-  exposure up/down 10%      z  100% zoom (focus check)
                 r    accept -> draw ROI -> record      q  quit
  2. ROI      - Pyvale window on a snapshot: draw ROI (include the notch),
                place the seed well inside, close the window
  3. RECORD   - camera runs at max frame rate for the crop (~478 fps at 400 rows)
                 * ~SAVE_FPS frames/s are saved continuously
                 * EVERY frame of the last BUFFER_SECONDS is kept in RAM
                 m  marker
                 c  crack! -> saves the full-rate RAM buffer AND keeps saving
                    every frame for POST_SECONDS (press again if needed)
                 q  stop (also saves the last BREAK_SECONDS of the RAM buffer at full rate)
                Frame 0 is the DIC reference: start with the specimen unloaded.
  4. AFTER    - frames finish writing, the frame viewer (view_frames.py) opens
                on the crack window, then DIC runs (overview at
                OVERVIEW_DIC_FPS, then every CRACK_DIC_STEP-th frame of the
                crack window), then pyvale_dic_run.py (strain, plots,
                animation). LIVE_DIC = True also runs DIC during the test
                (laptop); on the Pi leave it off so the cores stay free for
                capture.

Frames are named by their true frame number (frame_000123.tif), so the
continuous and full-rate parts fit together; frames.csv lists every frame
grabbed and whether it was saved.

Run on the Pi desktop, in the camera environment (README.md):
    source ~/VSBC_driver/.venv-camera/bin/activate
    python3 ~/VSBC_driver/camera/capture_dic_live.py
or on the laptop from Miniforge Prompt (close pylon Viewer first):
    conda activate dic
    python capture_dic_live.py
Results: <OUTPUT_ROOT>/<date>_<time>_<TEST_NAME>/

The vsbc GUI (Camera tab) uses this file too: its USER SETTINGS as defaults,
configure_camera() to set the camera up and dic_worker() for the DIC. Keep
those names when you update the file (README.md, "Interface used by the GUI").
Changes made while porting it to the GUI are marked "FIX:".
"""

import os
import json
import time
import queue
import threading
import datetime
import collections
import multiprocessing as mp
from pathlib import Path

import numpy as np
import cv2


# ---------------------------------------------------------------------------
# USER SETTINGS
# ---------------------------------------------------------------------------
TEST_NAME = "test01"
APERTURE_LABEL = "f2.8"

EXPOSURE_US = 1488          # starting value; adjustable in the preview
FPS = None                  # None = camera maximum (~169 fps at full size)
SAVE_FPS = 10               # continuous saving rate during loading (frames/s);
                            # the crack window is still saved at the full rate.
                            # 10 fps at 400 rows is ~8 MB/s, which a microSD keeps up with
SAVE_EVERY = 9              # vsbc GUI only ("every k-th frame" save mode); this
                            # script derives its step from SAVE_FPS
BUFFER_SECONDS = 2          # every frame of the last N s kept in RAM (covers
                            # reaction time: a brittle crack is over before 'c')
POST_SECONDS = 1            # after 'c', save every frame for N more s (q ends it early)
BREAK_SECONDS = 0.5         # on stop (q), only the last N s of the RAM buffer are saved
                            # at full rate (the vsbc GUI uses its own "Before the break" setting)
MAX_SECONDS = 1200          # safety stop (recording only), 20 min. FIX: was 45 s,
                            # which ended a tensile test early
SETTLE_SECONDS = 5          # wait after the ROI window closes before frame 0
                            # (hands off the table!) so the reference frame
                            # isn't taken while the setup is still shaking
CROP = dict(width=1920, height=400, offset_x=8, offset_y=408)   # from crop_fps_test.py (478 fps);
                            # set offset_y from crop_fps_test.py so the band covers the notch
                            # None = full 1920 x 1200 (169 fps)

# FIX: on Linux a Windows path is a relative folder literally named "C:\...";
# use the home folder there (the after-test scripts look in the same place).
OUTPUT_ROOT = (r"C:\VSBC_DIC_Test\captures" if os.name == "nt"
               else os.path.expanduser("~/vsbc_captures"))
REUSE_ROI = False           # True = reuse OUTPUT_ROOT/last_roi.npz (camera not moved)

# DIC
OVERVIEW_DIC_FPS = 2        # DIC rate over the whole (slow) test, frames/s;
                            # the crack window still gets every frame
DIC_STEP = 27               # vsbc GUI only (overview = every DIC_STEP/SAVE_EVERY-th
                            # saved frame); this script derives it from OVERVIEW_DIC_FPS
CRACK_DIC_STEP = 1          # full-rate crack window(s): 1 = every frame (~478 fps cropped)
                            # done AFTER the whole-test pass, so you get the
                            # overview first
subset_size = 41
subset_step = 20
max_displacement = 64
DIC_THREADS = max(1, (os.cpu_count() or 4) - 2)   # DIC during a test; after the
                                                  # test this script uses all cores
FILL_BATCH = 25
LIVE_DIC = False            # True = DIC on the newest frame during the test too
                            # (laptop). Off on the Pi: the cores are needed for capture

N_WRITERS = 4
PREVIEW_SCALE = 0.4
PREVIEW_EVERY_S = 0.1
LIVE_DIC_WINDOW = True      # live displacement map (only with LIVE_DIC)
SHOW_WINDOWS = True         # False only for testing without a screen
RUN_ANALYSIS_AFTER = True   # after DIC: run pyvale_dic_run.py on this run
                            # (strain, plots saved as PNG, strain animation)
PLAYBACK_AFTER_CAPTURE = True   # open view_frames.py on the crack window as soon
                                # as the frames are saved (DIC keeps running)


# ---------------------------------------------------------------------------
# DIC worker (separate process)
# ---------------------------------------------------------------------------
def dic_worker(cfg, done_q, status_q):
    import io
    import contextlib
    import pyvale.dic as dic

    frames_dir, out_dir, step = cfg["frames_dir"], cfg["out_dir"], cfg["step"]
    out_base = out_dir + os.sep

    def path_of(i):
        return os.path.join(frames_dir, f"frame_{i:06d}.tif")

    saved, processed = set(), set()
    crack_frames = set()
    finishing = False
    ref = path_of(0)
    t_per_frame = []
    vmax_seen = 0.5

    def run(batch):
        t0 = time.perf_counter()
        dic.calculate_2d(
            reference=ref,
            deformed=[Path(path_of(i)) for i in batch],
            roi_mask=cfg["roi_mask"],
            seed=cfg["roi_seed"],
            subset_size=cfg["subset_size"],
            subset_step=cfg["subset_step"],
            max_displacement=cfg["max_displacement"],
            num_threads=cfg["threads"],
            output_basepath=out_base,
            print_level=0,
        )
        t_per_frame.append((time.perf_counter() - t0) / len(batch))

    def show(i):
        nonlocal vmax_seen
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            res = dic.import_2d(
                data=os.path.join(out_dir, f"dic_results_frame_{i:06d}.csv"),
                delimiter=",", binary=False)
        conv = np.asarray(res.converged).astype(bool)[0]
        u = np.where(conv, np.asarray(res.u_px, float)[0], np.nan)
        v = np.where(conv, np.asarray(res.v_px, float)[0], np.nan)
        mag = np.hypot(u, v)
        status_q.put(("frame", i, float(np.nanmean(mag)),
                      float(np.nanmax(mag)), float(conv.mean()),
                      float(np.mean(t_per_frame[-5:]))))
        if not cfg["window"]:
            return
        vmax_seen = max(vmax_seen, float(np.nanmax(np.abs(v))))
        norm = np.clip((v / vmax_seen + 1.0) * 127.5, 0, 255)
        norm = np.nan_to_num(norm, nan=0).astype(np.uint8)
        col = cv2.applyColorMap(norm, cv2.COLORMAP_JET)
        col[~conv] = 0
        h, w = col.shape[:2]
        col = cv2.resize(col, (700, max(1, int(h * 700.0 / w))),
                         interpolation=cv2.INTER_NEAREST)
        cv2.putText(col, f"v (px) frame {i}  range +/-{vmax_seen:.2f}  "
                    f"valid {100 * conv.mean():.0f}%", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        cv2.imshow("live DIC: vertical displacement", col)
        cv2.waitKey(1)

    while True:
        try:
            while True:
                item = done_q.get_nowait()
                if item is None:
                    finishing = True
                elif isinstance(item, tuple) and item[0] == "crack":
                    crack_frames.update(item[1])
                else:
                    saved.add(item)
        except queue.Empty:
            pass

        if 0 not in saved:
            if finishing:
                status_q.put(("error", "reference frame never saved"))
                return
            time.sleep(0.02)
            continue

        grid = [i for i in saved if i > 0 and i % step == 0 and i not in processed]
        if not grid and finishing:
            # whole-test pass done -> dense pass over the crack window(s)
            grid = [i for i in crack_frames if i > 0 and i in saved
                    and i not in processed]
        if not grid:
            if finishing:
                break
            time.sleep(0.02)
            continue

        if not finishing and not cfg.get("live", True):
            time.sleep(0.05)                    # live DIC off: wait for the stop
            continue
        batch = sorted(grid)[:cfg["fill_batch"]] if finishing else [max(grid)]
        try:
            run(batch)
            processed.update(batch)
            show(batch[-1])
        except Exception as err:
            processed.update(batch)
            status_q.put(("error", f"frames {batch[0]}-{batch[-1]}: {err}"))
        if finishing:
            left = len([i for i in saved if i > 0 and i not in processed
                        and (i % step == 0 or i in crack_frames)])
            status_q.put(("fill", left))

    status_q.put(("done", len(processed),
                  float(np.mean(t_per_frame)) if t_per_frame else float("nan")))
    if cfg["window"]:
        cv2.waitKey(1500)
        cv2.destroyAllWindows()


# ---------------------------------------------------------------------------
# Camera setup (also used by the vsbc GUI's camera process)
# FIX: moved out of main() so the GUI can call them.
# ---------------------------------------------------------------------------
def _feature_errors():
    """GenICam errors (pypylon is only imported when a camera is used)."""
    from pypylon import genicam
    return (AttributeError, genicam.GenericException)


def set_feature(cam, name, value, quiet=False):
    try:
        node = getattr(cam, name)
        node.Value = value
        if not quiet:
            print(f"  {name} = {node.Value}")
        return True
    except _feature_errors() as err:
        print(f"  {name}: not set ({type(err).__name__}: {err})")
        return False


def get_feature(cam, *names):
    for name in names:
        try:
            return getattr(cam, name).Value
        except _feature_errors():
            continue
    return None


def round_to_inc(cam, name, value):
    try:
        node = getattr(cam, name)
        inc = int(node.Inc)
        # FIX: also clamp to Max. An offset past Max (offset_x = 8 with
        # width = 1920 when the sensor is 1920 wide) was only reported as
        # "not set", leaving the band somewhere else.
        return min(int(node.Max), max(int(node.Min), (int(value) // inc) * inc))
    except Exception:
        return int(value)


def fps_now(cam):
    return get_feature(cam, "BslResultingAcquisitionFrameRate",
                       "ResultingFrameRate")


def measure_fps(cam, n=60):
    """Frame rate from the camera's own timestamps, for models without a
    resulting-frame-rate node. Changes no settings."""
    from pypylon import pylon
    stamps = []
    cam.StartGrabbingMax(n, pylon.GrabStrategy_OneByOne)
    try:
        while cam.IsGrabbing():
            res = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            try:
                if res.GrabSucceeded():
                    stamps.append(int(res.TimeStamp))
            finally:
                res.Release()
    finally:
        cam.StopGrabbing()
    if len(stamps) < 2:
        return None
    return 1e9 / float(np.median(np.diff(stamps)))


def open_camera():
    from pypylon import pylon
    camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    camera.Open()
    return camera


def configure_camera(camera, exposure_us=EXPOSURE_US, crop=CROP, fps=FPS, quiet=False):
    """Mono8, no automatic exposure or gain, the crop, the exposure and the
    frame rate. Returns (width, height)."""
    set_feature(camera, "PixelFormat", "Mono8", quiet)
    set_feature(camera, "ExposureAuto", "Off", quiet)
    set_feature(camera, "GainAuto", "Off", quiet)
    set_feature(camera, "Gain", 0.0, quiet)
    set_feature(camera, "DeviceLinkThroughputLimitMode", "Off", quiet)
    if crop is not None:
        set_feature(camera, "OffsetX", 0, quiet)
        set_feature(camera, "OffsetY", 0, quiet)
        set_feature(camera, "Width", round_to_inc(camera, "Width", crop["width"]), quiet)
        set_feature(camera, "Height", round_to_inc(camera, "Height", crop["height"]), quiet)
        for name, key in (("OffsetX", "offset_x"), ("OffsetY", "offset_y")):
            wanted = int(crop[key])
            value = round_to_inc(camera, name, wanted)
            if value != wanted:
                print(f"  WARNING: {name} {wanted} does not fit this crop on the sensor; using {value}")
            set_feature(camera, name, value, quiet)
    set_feature(camera, "ExposureTime", float(exposure_us), quiet)
    if fps is None:
        set_feature(camera, "AcquisitionFrameRateEnable", False, quiet)
    else:
        set_feature(camera, "AcquisitionFrameRateEnable", True, quiet)
        set_feature(camera, "AcquisitionFrameRate", float(fps), quiet)
    return get_feature(camera, "Width"), get_feature(camera, "Height")


def frame_runs(frames):
    """[[first, last], ...] of consecutive frame numbers."""
    runs = []
    for i in sorted(frames):
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return runs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    from pypylon import pylon
    import pyvale.dic as dic

    # --- folders ---
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_folder = os.path.join(OUTPUT_ROOT, f"{stamp}_{TEST_NAME}")
    frames_folder = os.path.join(run_folder, "images")
    dic_folder = os.path.join(run_folder, "pyvale_results")
    os.makedirs(frames_folder, exist_ok=True)
    os.makedirs(dic_folder, exist_ok=True)
    print(f"Saving to {run_folder}")

    # --- camera ---
    camera = open_camera()
    info = camera.GetDeviceInfo()
    print(f"Opened {info.GetModelName()} (serial {info.GetSerialNumber()})")
    print("\nConfiguring:")
    exposure = float(EXPOSURE_US)
    width, height = configure_camera(camera, exposure, CROP, FPS)

    show = SHOW_WINDOWS

    # =======================================================================
    # 1. PREVIEW
    # =======================================================================
    print("\nPREVIEW: +/- exposure, z zoom, r = accept and continue, q = quit")
    win_p = "PREVIEW  (+/- exposure, z zoom, r = start, q = quit)"
    zoom = False
    camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
    accepted = False
    t_end_test = time.perf_counter() + 2.0      # used only when show = False
    try:
        while camera.IsGrabbing():
            res = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            try:
                if not res.GrabSucceeded():
                    continue
                img = np.array(res.Array, copy=True)
            finally:
                res.Release()
            if not show:
                if time.perf_counter() > t_end_test:
                    accepted = True
                    break
                continue
            sat = 100.0 * np.mean(img >= 255)
            p999 = float(np.percentile(img[::4, ::4], 99.9))
            if zoom:
                h, w = img.shape
                disp = img[h // 2 - 250:h // 2 + 250, w // 2 - 400:w // 2 + 400].copy()
            else:
                disp = cv2.resize(img, None, fx=PREVIEW_SCALE, fy=PREVIEW_SCALE,
                                  interpolation=cv2.INTER_AREA)
            fps_shown = fps_now(camera)     # FIX: None on models without that node
            cv2.putText(disp, f"exp {exposure:.0f} us  "
                        + (f"{fps_shown:.0f} fps  " if fps_shown else "")
                        + f"bright99.9 {p999:.0f}  sat {sat:.2f}%"
                        + ("  [100% zoom]" if zoom else ""),
                        (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 255, 2)
            cv2.imshow(win_p, disp)
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("+"), ord("=")):
                exposure *= 1.1
                set_feature(camera, "ExposureTime", exposure, quiet=True)
            elif key in (ord("-"), ord("_")):
                exposure /= 1.1
                set_feature(camera, "ExposureTime", exposure, quiet=True)
            elif key == ord("z"):
                zoom = not zoom
            elif key == ord("r"):
                accepted = True
                break
            elif key in (ord("q"), 27):
                break
    finally:
        camera.StopGrabbing()
        if show:
            cv2.destroyAllWindows()
    if not accepted:
        camera.Close()
        print("Quit from preview; nothing recorded.")
        return
    exposure = float(get_feature(camera, "ExposureTime"))
    fps_cam = fps_now(camera)
    if not fps_cam:
        # FIX: no resulting-frame-rate node: measure it (the buffer size depends on it)
        fps_cam = measure_fps(camera) or 100.0
    frame_mb = width * height / 1e6
    n_buffer = int(BUFFER_SECONDS * fps_cam)
    print(f"\nAccepted: exposure {exposure:.0f} us, {width} x {height} at "
          f"{fps_cam:.1f} fps")
    # Steps from the rates, so they follow the crop's frame rate; DIC_STEP is a
    # multiple of SAVE_EVERY, so every overview frame is one that was saved.
    SAVE_EVERY = max(1, int(round(fps_cam / SAVE_FPS)))
    DIC_STEP = SAVE_EVERY * max(1, int(round((fps_cam / SAVE_EVERY) / OVERVIEW_DIC_FPS)))
    gb_per_min = frame_mb * fps_cam / SAVE_EVERY * 60 / 1000
    print(f"  continuous save: every {SAVE_EVERY} -> {fps_cam / SAVE_EVERY:.1f} fps "
          f"({frame_mb * fps_cam / SAVE_EVERY:.0f} MB/s, {gb_per_min:.2f} GB per minute)")
    print(f"  overview DIC: every {DIC_STEP} -> {fps_cam / DIC_STEP:.1f} fps")
    try:
        import shutil
        free_gb = shutil.disk_usage(run_folder).free / 1e9
        print(f"  disk free: {free_gb:.0f} GB -> room for about "
              f"{free_gb / max(gb_per_min, 1e-6):.0f} min of recording")
    except OSError:
        pass
    print(f"  full-rate RAM buffer: {BUFFER_SECONDS} s = {n_buffer} frames "
          f"= {n_buffer * frame_mb / 1000:.1f} GB of RAM")

    # =======================================================================
    # 2. ROI
    # =======================================================================
    roi_file = os.path.join(OUTPUT_ROOT, "last_roi.npz")
    if REUSE_ROI and os.path.exists(roi_file):
        saved_roi = np.load(roi_file, allow_pickle=True)
        roi_mask = saved_roi["mask"]
        roi_seed = saved_roi["seed"].tolist()
        print(f"\nReusing ROI from {roi_file}")
    else:
        res = camera.GrabOne(5000)
        try:
            if not res.GrabSucceeded():      # FIX: .Array of a failed grab raises
                raise RuntimeError(f"Snapshot for the ROI failed: {res.ErrorDescription}")
            snap = np.array(res.Array, copy=True)
        finally:
            res.Release()
        snap_path = os.path.join(run_folder, "roi_snapshot.tif")
        cv2.imwrite(snap_path, snap)
        print("\nDraw the ROI (include the notch area), place the seed well "
              "inside it, then close the window.")
        roi = dic.RegionOfInterest(snap_path)
        roi.interactive_selection()
        roi_mask = np.asarray(roi.mask)
        roi_seed = roi.seed
        np.savez(roi_file, mask=roi_mask, seed=np.array(roi_seed, dtype=object))
    # calculate_2d wants one seed as [x, y] ints (the ROI window can return a
    # list of points, or floats)
    _p = np.asarray(roi_seed, dtype=float).reshape(-1, 2)[0]
    roi_seed = [int(round(_p[0])), int(round(_p[1]))]
    if tuple(roi_mask.shape) != (height, width):
        raise RuntimeError(f"ROI mask {roi_mask.shape} does not match image "
                           f"{(height, width)}; set REUSE_ROI = False")

    settings = {
        "test_name": TEST_NAME, "aperture": APERTURE_LABEL,
        "exposure_us": exposure, "gain": 0.0, "fps_requested": FPS,
        "fps_camera": fps_cam, "width": width, "height": height,
        "offset_x": get_feature(camera, "OffsetX"),
        "offset_y": get_feature(camera, "OffsetY"),
        "save_every": SAVE_EVERY, "save_fps": fps_cam / SAVE_EVERY,
        "buffer_seconds": BUFFER_SECONDS, "break_seconds": BREAK_SECONDS,
        "post_seconds": POST_SECONDS,
        "model": info.GetModelName(), "serial": info.GetSerialNumber(),
        "camera_temperature_c_start": get_feature(camera, "DeviceTemperature"),
        "dic": dict(step=DIC_STEP, subset_size=subset_size,
                    subset_step=subset_step, max_displacement=max_displacement,
                    threads=DIC_THREADS),
    }

    # =======================================================================
    # 3. RECORD (+ 4. DIC in a separate process)
    # =======================================================================
    ctx = mp.get_context("spawn")
    done_q, status_q = ctx.Queue(), ctx.Queue()
    cfg = dict(frames_dir=frames_folder, out_dir=dic_folder, step=DIC_STEP,
               roi_mask=roi_mask, roi_seed=roi_seed, subset_size=subset_size,
               subset_step=subset_step, max_displacement=max_displacement,
               threads=DIC_THREADS if LIVE_DIC else (os.cpu_count() or 4),
               fill_batch=FILL_BATCH,
               window=LIVE_DIC and LIVE_DIC_WINDOW and show, live=LIVE_DIC)
    worker = ctx.Process(target=dic_worker, args=(cfg, done_q, status_q),
                         daemon=True)
    worker.start()

    # Unbounded queue: frames are already in RAM; a burst from the buffer
    # must never block grabbing.
    save_q = queue.Queue()
    write_errors = []
    enqueued = set()

    def writer():
        while True:
            item = save_q.get()
            if item is None:
                save_q.task_done()
                break
            i, im = item
            try:
                if cv2.imwrite(os.path.join(frames_folder, f"frame_{i:06d}.tif"), im):
                    done_q.put(i)
                else:
                    write_errors.append(i)
            except Exception:
                write_errors.append(i)
            save_q.task_done()

    writers = [threading.Thread(target=writer, daemon=True) for _ in range(N_WRITERS)]
    for w in writers:
        w.start()

    def enqueue(i, im):
        if i not in enqueued:
            enqueued.add(i)
            save_q.put((i, im))

    ring = collections.deque(maxlen=max(1, n_buffer))
    rows, markers = [], []
    pending_marker = ""
    full_rate_until = -1.0
    # FIX: the crack windows are recorded as they happen. They used to be
    # guessed afterwards from runs of consecutive saved frames, which made
    # the whole recording one "crack window" with SAVE_EVERY = 1.
    full_rate_frames = set()
    crack_dumps = 0
    max_queue = grab_errors = 0
    queue_warn_at = max(50, int(2 * fps_cam))
    queue_warned = write_warned = False
    dic_status = "DIC: waiting" if LIVE_DIC else "DIC: runs after you stop"
    dic_errors = []

    def add_marker(i, t, label):
        nonlocal pending_marker
        markers.append((i, t, label))
        pending_marker = label
        print(f"  {label} at frame {i} ({t:.2f} s)")

    def dump_buffer(last=None):
        n = 0
        frames = list(ring)
        if last is not None:
            frames = frames[-max(1, int(round(last * fps_cam))):]
        for i, im in frames:
            full_rate_frames.add(i)
            if i not in enqueued:
                enqueue(i, im)
                n += 1
        return n

    def poll_status():
        nonlocal dic_status
        try:
            while True:
                msg = status_q.get_nowait()
                if msg[0] == "frame":
                    _, i, mean_u, max_u, valid, tpf = msg
                    dic_status = (f"DIC frame {i}: mean|u| {mean_u:.2f} px, "
                                  f"max {max_u:.2f}, valid {100 * valid:.0f}%, "
                                  f"{tpf:.2f} s/frame")
                elif msg[0] == "error":
                    dic_errors.append(msg[1])
                    print(f"  DIC error: {msg[1]}")
                elif msg[0] == "fill":
                    print(f"  DIC filling in: {msg[1]} frames left   ", end="\r")
                elif msg[0] == "done":
                    print(f"\n  DIC finished: {msg[1]} frames, "
                          f"{msg[2]:.2f} s/frame average")
        except queue.Empty:
            pass

    if SETTLE_SECONDS > 0:
        print(f"\nSettling {SETTLE_SECONDS} s before the reference frame - "
              "don't touch the table or laptop...")
        time.sleep(SETTLE_SECONDS)
    camera.MaxNumBuffer.Value = 300
    camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
    t0 = time.perf_counter()
    last_preview = 0.0
    idx = 0
    win = "RECORDING  (m = marker, c = CRACK, q = stop)"
    print("\nRECORDING (frame 0 = DIC reference). m = marker, c = crack, q = stop.\n")

    try:
        while camera.IsGrabbing():
            res = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            try:
                if not res.GrabSucceeded():
                    grab_errors += 1
                    continue
                img = np.array(res.Array, copy=True)
                try:
                    block_id = int(res.BlockID)
                except Exception:
                    block_id = -1
                try:
                    cam_ts = int(res.TimeStamp)
                except Exception:
                    cam_ts = -1
            finally:
                res.Release()

            t_pc = time.perf_counter() - t0
            rows.append([idx, block_id, cam_ts, t_pc, pending_marker])
            pending_marker = ""
            ring.append((idx, img))
            if idx % SAVE_EVERY == 0 or t_pc < full_rate_until:
                enqueue(idx, img)
            if t_pc < full_rate_until:
                full_rate_frames.add(idx)
            waiting = save_q.qsize()
            max_queue = max(max_queue, waiting)
            if waiting > queue_warn_at and not queue_warned:
                # FIX: the queue is unbounded, so a slow disk (SD card) fills
                # RAM until the program is killed. Say so while it happens.
                queue_warned = True
                print(f"  WARNING: {waiting} frames waiting to be written: the disk is slower than the "
                      "save rate and RAM keeps filling. Lower SAVE_FPS or save to a faster drive.")
            idx += 1

            if t_pc - last_preview >= PREVIEW_EVERY_S:
                last_preview = t_pc
                poll_status()
                if write_errors and not write_warned:
                    # FIX: write errors (disk full) were only counted at the end
                    write_warned = True
                    print(f"  WARNING: frame {write_errors[0]} could not be written (disk full?). "
                          "Check the drive.")
                key = -1
                if show:
                    small = cv2.resize(img, None, fx=PREVIEW_SCALE,
                                       fy=PREVIEW_SCALE,
                                       interpolation=cv2.INTER_AREA)
                    mode = "FULL RATE" if t_pc < full_rate_until else f"1/{SAVE_EVERY}"
                    cv2.putText(small, f"{t_pc:5.1f} s  frame {idx}  saving {mode}"
                                f"  queue {save_q.qsize()}", (10, 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, 255, 2)
                    cv2.putText(small, dic_status, (10, 50),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, 255, 1)
                    cv2.imshow(win, small)
                    key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("m"):
                    add_marker(idx, t_pc, f"marker{len(markers) + 1}")
                if key == ord("c"):
                    crack_dumps += 1
                    add_marker(idx, t_pc, f"crack{crack_dumps}")
                    n = dump_buffer()
                    full_rate_until = t_pc + POST_SECONDS
                    print(f"    saving {n} buffered full-rate frames + "
                          f"{POST_SECONDS} s more at full rate")
            if t_pc > MAX_SECONDS:
                print("MAX_SECONDS reached")
                break
    except KeyboardInterrupt:
        print("Stopped with Ctrl+C")
    finally:
        camera.StopGrabbing()
        settings["camera_temperature_c_end"] = get_feature(camera, "DeviceTemperature")
        camera.Close()
        if show:
            cv2.destroyAllWindows()

    t_total = time.perf_counter() - t0
    last_s = min(BREAK_SECONDS, BUFFER_SECONDS)
    n_dump = dump_buffer(last_s)
    print(f"\nStopped after {t_total:.1f} s, {idx} frames grabbed. "
          f"Saving last {last_s} s at full rate ({n_dump} more frames)...")
    while save_q.unfinished_tasks:
        print(f"  {save_q.qsize()} frames left to write   ", end="\r")
        poll_status()
        time.sleep(0.5)
    save_q.join()
    for _ in writers:
        save_q.put(None)
    for w in writers:
        w.join()

    # Full-rate crack window(s)
    bad = set(write_errors)       # FIX: built once (it was rebuilt for every frame)
    crack_list = sorted(i for i in full_rate_frames if i in enqueued and i not in bad)
    windows = frame_runs(crack_list)
    crack_list = crack_list[::CRACK_DIC_STEP]
    if crack_list:
        done_q.put(("crack", crack_list))
        print(f"Full-rate crack window: frames {crack_list[0]}-{crack_list[-1]} "
              f"({len(crack_list)} frames for dense DIC)")
    done_q.put(None)

    # --- metadata ---
    ids = np.array([r[1] for r in rows])
    dropped = (int(np.sum(np.diff(ids) - 1))
               if len(ids) > 1 and ids.min() >= 0 else None)
    ts = np.array([r[2] for r in rows], dtype=float)
    fps_meas = (1.0 / (np.median(np.diff(ts)) * 1e-9)
                if len(ts) > 1 and ts.min() >= 0 else idx / t_total)
    with open(os.path.join(run_folder, "frames.csv"), "w") as fh:
        fh.write("frame_index,filename,saved,block_id,camera_timestamp_ns,"
                 "pc_time_s,marker\n")
        for r in rows:
            s = int(r[0] in enqueued and r[0] not in bad)
            fh.write(f"{r[0]},frame_{r[0]:06d}.tif,{s},{r[1]},{r[2]},"
                     f"{r[3]:.6f},{r[4]}\n")
    n_saved = len(enqueued) - len(bad)
    settings.update({
        "frames_grabbed": idx, "frames_saved": n_saved,
        "duration_s": t_total, "fps_measured": fps_meas,
        "dropped_frames": dropped, "grab_errors": grab_errors,
        "write_errors": len(bad), "max_save_queue": max_queue,
        "markers": [{"frame": m[0], "pc_time_s": m[1], "label": m[2]}
                    for m in markers],
        "crack_windows": windows,
    })
    with open(os.path.join(run_folder, "capture_settings.json"), "w") as fh:
        json.dump(settings, fh, indent=2)

    print("\n=============== CAPTURE ===============")
    print(f"Grabbed: {idx} frames at {fps_meas:.1f} fps (camera clock)")
    print(f"Saved:   {n_saved} frames ({len(bad)} write errors)")
    print(f"Dropped frames: {dropped}   grab errors: {grab_errors}")
    print(f"Peak save queue: {max_queue} frames")
    print(f"Markers: {[(m[2], m[0]) for m in markers]}")
    print("=======================================")

    # --- look at the frames right away (DIC continues in the background) ---
    if PLAYBACK_AFTER_CAPTURE and show:
        try:
            import view_frames
        except ImportError:
            print("view_frames.py not found next to this script; skipping playback")
        else:
            print("\nOpening the frame viewer (space = play, c = crack window, "
                  "q = close). DIC keeps running meanwhile.")
            view_frames.view_run(run_folder)

    # --- wait for DIC fill-in ---
    n_target = len(set(i for i in enqueued if i > 0 and i % DIC_STEP == 0
                        and i not in bad) | set(i for i in crack_list if i > 0))
    print(f"\nDIC: every {DIC_STEP}th frame first, then the crack window "
          f"({n_target} frames total)...")
    while worker.is_alive():
        poll_status()
        time.sleep(0.2)
    poll_status()
    n_results = len([f for f in os.listdir(dic_folder)
                     if f.startswith("dic_results_")])
    print(f"\nDIC results: {n_results} / {n_target} frames in {dic_folder}")
    if dic_errors:
        print(f"{len(dic_errors)} DIC error(s); first: {dic_errors[0]}")
    analysis = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pyvale_dic_run.py")
    if RUN_ANALYSIS_AFTER and n_results > 0 and os.path.exists(analysis):
        import subprocess
        import sys
        print("\nRunning pyvale_dic_run.py on this run (strain, plots, animation)...")
        t_a = time.perf_counter()
        r = subprocess.run([sys.executable, analysis, run_folder])
        print(f"Analysis {'finished' if r.returncode == 0 else 'FAILED'} in "
              f"{time.perf_counter() - t_a:.0f} s. Plots + animation are in {dic_folder}")
    else:
        if RUN_ANALYSIS_AFTER and not os.path.exists(analysis):
            print("pyvale_dic_run.py not found next to this script.")
        print(f'\nNext:  python pyvale_dic_run.py "{run_folder}"')


if __name__ == "__main__":
    main()
