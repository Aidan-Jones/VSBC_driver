# -*- coding: utf-8 -*-
"""
view_frames.py

Look through the recorded frames of a run (no DIC needed).
Opens at the start of the full-rate crack window.

Controls (click the window first):
    space        play / pause
    d / a        next / previous frame        (D / A = 10 frames)
    ] / [        faster / slower playback
    c            jump to the start of the crack window
    0            jump to the first frame
    z            100% zoom (center) on/off
    e            export the crack window as a slow-motion video (.avi)
    q / Esc      quit
    slider       jump anywhere

On screen: frame number, time (frames.csv's time_s from the vsbc GUI, else
since frame 0), time relative to the 'c' press (ms), and whether you are in
the full-rate part.

Use:
    python3 view_frames.py                     -> newest run in RUNS_ROOT
    python3 view_frames.py ~/vsbc_captures/<run folder>
capture_dic_live.py also opens it automatically after saving.
"""

import os
import re
import csv
import sys
import glob
import collections

import numpy as np
import cv2

RUNS_ROOT = (r"C:\VSBC_DIC_Test\captures" if os.name == "nt"
             else os.path.expanduser("~/vsbc_captures"))
DISPLAY_WIDTH = 1200
PLAY_FPS = 30               # display speed; at 478 fps capture that is ~16x slow motion
EXPORT_FPS = 30


def _frame_num(path):
    m = re.search(r"frame[_\-]?(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else -1


def read_frames_csv(run_folder):
    """Rows of frames.csv. Read with the csv module: np.genfromtxt(dtype=None)
    guesses each column's type and misreads an empty marker column."""
    path = os.path.join(run_folder, "frames.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def frame_times(rows):
    """{frame: time in s}: the time_s column written by the vsbc GUI (the test's
    data.csv clock) when it is there, else the camera clock from the first frame."""
    if rows and rows[0].get("time_s") not in (None, ""):
        return {int(r["frame_index"]): float(r["time_s"]) for r in rows
                if r.get("time_s") not in (None, "")}
    ts = {int(r["frame_index"]): int(r["camera_timestamp_ns"]) for r in rows}
    if not ts or min(ts.values()) < 0:
        return {}
    t0 = ts[min(ts)]
    return {k: (v - t0) * 1e-9 for k, v in ts.items()}


def view_run(run_folder, show=True, scripted_keys=None):
    images = os.path.join(run_folder, "images")
    paths = sorted(glob.glob(os.path.join(images, "frame_*.tif")), key=_frame_num)
    if not paths:
        print(f"No frames in {images}")
        return
    nums = np.array([_frame_num(p) for p in paths])

    # times + markers from frames.csv
    rows = read_frames_csv(run_folder)
    t_of = frame_times(rows)
    markers = [(int(r["frame_index"]), r["marker"]) for r in rows if r.get("marker")]
    crack_marks = [f for f, m in markers if m.startswith("crack")]

    # full-rate window = consecutive frame numbers
    consec = np.zeros(len(nums), bool)
    if len(nums) > 1:
        d = np.diff(nums) == 1
        consec[:-1] |= d
        consec[1:] |= d
    crack_start = int(np.argmax(consec)) if consec.any() else 0
    crack_end = (len(nums) - 1 - int(np.argmax(consec[::-1]))) if consec.any() else len(nums) - 1
    print(f"{len(nums)} saved frames ({nums[0]}-{nums[-1]}); full-rate window: "
          f"{'frames %d-%d (%d frames)' % (nums[crack_start], nums[crack_end], crack_end - crack_start + 1) if consec.any() else 'none'}")

    cache = collections.OrderedDict()

    def load(i):
        if i in cache:
            cache.move_to_end(i)
            return cache[i]
        img = cv2.imread(paths[i], cv2.IMREAD_GRAYSCALE)
        cache[i] = img
        if len(cache) > 200:
            cache.popitem(last=False)
        return img

    def render(i, zoom):
        img = load(i)
        if zoom:
            h, w = img.shape
            hh, ww = min(h, 500), min(w, 900)
            view, scale = img[(h - hh) // 2:(h + hh) // 2, (w - ww) // 2:(w + ww) // 2], 1.0
        else:
            view, scale = img, DISPLAY_WIDTH / img.shape[1]
        disp = cv2.cvtColor(cv2.resize(view, None, fx=scale, fy=scale,
                                       interpolation=cv2.INTER_AREA), cv2.COLOR_GRAY2BGR)
        n = nums[i]
        txt = f"frame {n}  ({i + 1}/{len(nums)})"
        if n in t_of:
            txt += f"   t = {t_of[n]:.4f} s"
            if crack_marks and crack_marks[0] in t_of:
                txt += f"   {1000 * (t_of[n] - t_of[crack_marks[0]]):+.1f} ms from 'c'"
        if consec[i]:
            txt += "   FULL RATE"
        lab = [m for f, m in markers if f == n]
        if lab:
            txt += f"   <{lab[0]}>"
        cv2.putText(disp, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4)
        cv2.putText(disp, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
        return disp

    def export():
        out = os.path.join(run_folder, "crack_window.avi")
        first = render(crack_start, False)
        vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"MJPG"), EXPORT_FPS,
                             (first.shape[1], first.shape[0]))
        for i in range(crack_start, crack_end + 1):
            vw.write(render(i, False))
        vw.release()
        print(f"Exported {crack_end - crack_start + 1} frames to {out} "
              f"at {EXPORT_FPS} fps")

    keys = list(scripted_keys) if scripted_keys is not None else None
    win = f"Frames: {os.path.basename(os.path.normpath(run_folder))}"
    i, playing, zoom, play_fps = crack_start, False, False, PLAY_FPS
    state = {"jump": None}
    if show:
        cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
        cv2.createTrackbar("frame", win, i, max(1, len(nums) - 1),
                           lambda v: state.__setitem__("jump", v))
    while True:
        if state["jump"] is not None:
            i, state["jump"] = min(int(state["jump"]), len(nums) - 1), None
        disp = render(i, zoom)
        if show:
            cv2.imshow(win, disp)
            k = cv2.waitKey(max(1, int(1000 / play_fps)) if playing else 30) & 0xFF
        else:
            k = ord(keys.pop(0)) if keys else ord("q")
        if k in (ord("q"), 27):
            break
        elif k == ord(" "):
            playing = not playing
        elif k == ord("d"):
            i = min(len(nums) - 1, i + 1)
        elif k == ord("a"):
            i = max(0, i - 1)
        elif k == ord("D"):
            i = min(len(nums) - 1, i + 10)
        elif k == ord("A"):
            i = max(0, i - 10)
        elif k == ord("]"):
            play_fps = min(240, play_fps * 1.5)
        elif k == ord("["):
            play_fps = max(2, play_fps / 1.5)
        elif k == ord("c"):
            i = crack_start
        elif k == ord("0"):
            i = 0
        elif k == ord("z"):
            zoom = not zoom
        elif k == ord("e"):
            export()
        elif playing:
            i = i + 1 if i < len(nums) - 1 else i
            if i == len(nums) - 1:
                playing = False
        if show:
            cv2.setTrackbarPos("frame", win, i)
            state["jump"] = None
    if show:
        cv2.destroyWindow(win)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        folder = sys.argv[1]
    else:
        runs = sorted(d for d in glob.glob(os.path.join(RUNS_ROOT, "*"))
                      if os.path.isdir(os.path.join(d, "images")))
        if not runs:
            sys.exit(f"No runs in {RUNS_ROOT}")
        folder = runs[-1]
    print(f"Run: {folder}")
    view_run(folder)
