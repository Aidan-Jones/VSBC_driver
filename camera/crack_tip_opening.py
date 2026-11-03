# -*- coding: utf-8 -*-
"""
crack_tip_opening.py  (version for capture_dic_live.py runs)

Crack tip from DIC crack OPENING: the vertical displacement jump across the
crack line. Meant for brittle cracks, where the opening drops sharply to
zero at the tip. (It failed on the ductile material, where the opening
fades out over hundreds of pixels ahead of the tip.)

Per analyzed frame:
  opening(x) = v(just below the crack line) - v(just above)
  using the nearest valid DIC points at least subset_size/2 + 5 px away,
  so their subsets don't straddle the crack.
  Tip = last x (from the notch, in the crack direction) where the opening
  exceeds the threshold, allowing short gaps, interpolated between points.

Threshold = max(MIN_OPENING_PX, K_SIGMA * noise), where noise is measured
on PRE-CRACK frames (before the full-rate crack window). Unlike the old
version this works even when the crack runs all the way across, because
it doesn't rely on uncracked columns in the same frame.

Outputs <run>/crack_tip_opening.csv with time, tip (raw and no-backward),
crack length and crack speed, plus plots. Compares with
crack_tip_image.csv if it exists.

The vsbc GUI runs this file after each test with RUN_FOLDER, NOTCH_TIP,
CRACK_FROM_LEFT and subset_size set for that test (it rewrites those lines).
Changes made while porting it to the GUI are marked "FIX:".
"""

import os
import re
import csv
import glob
import json

import numpy as np
import matplotlib.pyplot as plt

import pyvale.dic as dic


# ---------------------------------------------------------------------------
# USER SETTINGS
# ---------------------------------------------------------------------------
RUN_FOLDER = r"C:\VSBC_DIC_Test\captures\YYYYMMDD_HHMMSS_test01"

NOTCH_TIP = None            # (x, y) in recorded-image pixels; None = auto line,
                            # tip search starts at the DIC grid edge
CRACK_FROM_LEFT = True
CRACK_Y = None              # crack line y; None = NOTCH_TIP y, or auto

subset_size = 41            # must match the DIC run
K_SIGMA = 5.0
MIN_OPENING_PX = 0.3        # threshold is never lower than this
MAX_GAP_COLS = 1
MAX_ROW_SEARCH = 3
SPEED_SMOOTH_FRAMES = 5     # smoothing for crack speed (dense window)


# ---------------------------------------------------------------------------
def frame_num(name):
    m = re.search(r"frame[_\-]?(\d+)", str(name))
    return int(m.group(1)) if m else None


def first_valid(frame, rows):
    out = np.full(frame.shape[1], np.nan)
    for i in rows:
        fill = np.isnan(out) & np.isfinite(frame[i])
        out[fill] = frame[i][fill]
    return out


def read_frames_csv(run_folder):
    path = os.path.join(run_folder, "frames.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def frame_times(rows):
    """{frame: time in s}. FIX: uses the time_s column written by the vsbc GUI
    (the same clock and zero as the test's data.csv) when it is there, else the
    camera clock counted from frame 0 as before."""
    if rows and rows[0].get("time_s") not in (None, ""):
        return {int(r["frame_index"]): float(r["time_s"]) for r in rows
                if r.get("time_s") not in (None, "")}
    ts = {int(r["frame_index"]): int(r["camera_timestamp_ns"]) for r in rows}
    if not ts or min(ts.values()) < 0 or 0 not in ts:
        return {}
    return {k: (v - ts[0]) * 1e-9 for k, v in ts.items()}


def dense_pairs(nums, run_folder):
    """Indices k where analyzed frames k and k+1 both lie in a full-rate window.

    FIX: the windows come from capture_settings.json (crack_windows from
    capture_dic_live.py, full_rate_windows from the vsbc GUI). This used to
    look for consecutive frame numbers only, which finds no window when
    CRACK_DIC_STEP > 1. Without the file: the smallest step between analyzed
    frames, when it is smaller than the usual one.
    """
    windows = []
    path = os.path.join(run_folder, "capture_settings.json")
    if os.path.exists(path):
        with open(path) as fh:
            meta = json.load(fh)
        windows = meta.get("full_rate_windows") or meta.get("crack_windows") or []
    if windows:
        inside = np.zeros(len(nums), bool)
        for a, b in windows:
            inside |= (nums >= a) & (nums <= b)
        return np.where(inside[:-1] & inside[1:])[0]
    dn = np.diff(nums)
    if dn.size and dn.min() < np.median(dn):
        return np.where(dn == dn.min())[0]
    return np.array([], dtype=int)


def main():
    results = os.path.join(RUN_FOLDER, "pyvale_results")
    files = sorted(glob.glob(os.path.join(results, "dic_results_*")))
    assert files, f"No DIC results in {results}"
    nums = np.array([frame_num(os.path.basename(f)) for f in files])

    d = dic.import_2d(data=os.path.join(results, "dic_results_*"),
                      delimiter=",", binary=False)
    V = np.where(np.asarray(d.converged).astype(bool),
                 np.asarray(d.v_px, float), np.nan)
    ssx = np.asarray(d.ss_x, float)
    ssy = np.asarray(d.ss_y, float)
    gx = ssx[0, :] if ssx.ndim == 2 else ssx
    gy = ssy[:, 0] if ssy.ndim == 2 else ssy
    n = V.shape[0]
    assert n == len(nums)

    # --- time + crack marker from frames.csv ---
    t = nums.astype(float)
    have_time = False
    crack_marks = []
    rows = read_frames_csv(RUN_FOLDER)
    if rows:
        t_of = frame_times(rows)
        if all(k in t_of for k in nums):
            t = np.array([t_of[k] for k in nums])
            have_time = True
        crack_marks = [int(r["frame_index"]) for r in rows
                       if (r.get("marker") or "").startswith("crack")]

    # --- orientation ---
    if not CRACK_FROM_LEFT:
        V = V[:, :, ::-1]
        gx_s = gx[::-1]
    else:
        gx_s = gx
    if NOTCH_TIP is not None:
        sel = (gx_s >= NOTCH_TIP[0]) if CRACK_FROM_LEFT else (gx_s <= NOTCH_TIP[0])
        start_col = int(np.argmax(sel)) if sel.any() else 0
    else:
        start_col = 0

    # --- pre-crack frames: before the dense (full-rate) window ---
    dense = dense_pairs(nums, RUN_FOLDER)
    if dense.size:
        pre = np.arange(n) < dense[0]
    else:
        pre = np.arange(n) < max(3, n // 5)
    if pre.sum() < 3:
        pre = np.arange(n) < max(3, n // 5)

    # --- crack line ---
    if CRACK_Y is not None:
        y_c = float(CRACK_Y)
    elif NOTCH_TIP is not None:
        y_c = float(NOTCH_TIP[1])
    else:
        last = V[-1]
        order = np.argsort(gy)
        lastg = last[order]
        gy_s = gy[order]
        sc = np.full(gy.size - 1, np.nan)
        for b in range(gy.size - 1):
            a_ = first_valid(lastg, list(range(b, max(-1, b - MAX_ROW_SEARCH), -1)))
            b_ = first_valid(lastg, list(range(b + 1, min(gy.size, b + 1 + MAX_ROW_SEARCH))))
            dd = np.abs(b_ - a_)
            if np.isfinite(dd).sum() >= 3:
                sc[b] = np.nanmedian(dd)
        if not np.isfinite(sc).any():
            # FIX: this ended in "crack line too close to the ROI edge", which hid the cause
            raise RuntimeError("Could not find the crack line: the last frame has no opening "
                               "between any rows of DIC points. Set CRACK_Y or NOTCH_TIP.")
        best = np.where(sc >= 0.95 * np.nanmax(sc))[0]
        y_c = float(np.mean(0.5 * (gy_s[best] + gy_s[best + 1])))
    min_dist = subset_size / 2.0 + 5.0
    rows_above = [i for i in np.argsort(-gy) if y_c - gy[i] >= min_dist][:MAX_ROW_SEARCH]
    rows_below = [i for i in np.argsort(gy) if gy[i] - y_c >= min_dist][:MAX_ROW_SEARCH]
    assert rows_above and rows_below, "crack line too close to the ROI edge"

    opening = np.stack([first_valid(V[k], rows_below) - first_valid(V[k], rows_above)
                        for k in range(n)])

    # --- threshold from pre-crack noise ---
    pre_op = opening[pre]
    resid = pre_op - np.nanmedian(pre_op, axis=0)
    noise = 1.4826 * np.nanmedian(np.abs(resid - np.nanmedian(resid)))
    thr = max(MIN_OPENING_PX, K_SIGMA * noise)
    print(f"Crack line y = {y_c:.1f} px; rows above {[round(gy[i]) for i in rows_above]}, "
          f"below {[round(gy[i]) for i in rows_below]}")
    print(f"Pre-crack opening noise {noise:.3f} px ({pre.sum()} frames) -> "
          f"threshold {thr:.3f} px")

    def tip_of(op):
        above = op > thr
        first = start_col
        while first < op.size and not np.isfinite(op[first]):
            first += 1
        if first >= op.size or not above[first]:
            return np.nan
        last_in, gap = first, 0
        for j in range(first + 1, op.size):
            if above[j]:
                last_in, gap = j, 0
            else:
                gap += 1
                if gap > MAX_GAP_COLS:
                    break
        if last_in + 1 >= op.size or not np.isfinite(op[last_in + 1]):
            return gx_s[last_in]
        o1, o2 = op[last_in], op[last_in + 1]
        f = float(np.clip((o1 - thr) / (o1 - o2), 0, 1)) if o1 != o2 else 0.0
        return gx_s[last_in] + f * (gx_s[last_in + 1] - gx_s[last_in])

    tip_raw = np.array([tip_of(opening[k]) for k in range(n)])
    tip = tip_raw.copy()
    best = np.nan
    for k in range(n):
        if np.isfinite(tip_raw[k]):
            best = tip_raw[k] if np.isnan(best) else (
                max(best, tip_raw[k]) if CRACK_FROM_LEFT else min(best, tip_raw[k]))
        tip[k] = best
    x0 = NOTCH_TIP[0] if NOTCH_TIP is not None else gx_s[start_col]
    length = np.abs(tip - x0)

    # crack speed in the dense window (px/s), smoothed
    speed = np.full(n, np.nan)
    if have_time:
        L = np.where(np.isfinite(length), length, 0.0)
        k = SPEED_SMOOTH_FRAMES
        Ls = np.convolve(L, np.ones(k) / k, mode="same")
        dt = np.gradient(t)
        speed = np.gradient(Ls) / np.where(dt > 0, dt, np.nan)
        speed[~np.isin(np.arange(n), np.concatenate([dense, dense + 1]))] = np.nan

    out_csv = os.path.join(RUN_FOLDER, "crack_tip_opening.csv")
    with open(out_csv, "w") as fh:
        fh.write("frame_index,time_s,tip_x_raw_px,tip_x_px,crack_length_px,"
                 "crack_speed_px_s,threshold_px,crack_y_px\n")
        for k in range(n):
            fh.write(f"{nums[k]},{t[k]:.6f},{tip_raw[k]:.2f},{tip[k]:.2f},"
                     f"{length[k]:.2f},{speed[k]:.1f},{thr:.4f},{y_c:.1f}\n")
    print(f"Tip found in {np.isfinite(tip_raw).sum()}/{n} frames. Saved {out_csv}")
    if np.isfinite(speed).any():
        print(f"Peak crack speed (smoothed): {np.nanmax(speed):.0f} px/s")

    # --- compare with the image tracker ---
    img_csv = os.path.join(RUN_FOLDER, "crack_tip_image.csv")
    img = None
    if os.path.exists(img_csv):
        img = np.genfromtxt(img_csv, delimiter=",", names=True)
        lk = dict(zip(img["frame_index"].astype(int), img["crack_length_px"]))
        dd = np.array([length[k] - lk[nums[k]] for k in range(n)
                       if nums[k] in lk and np.isfinite(length[k])])
        if dd.size:
            print(f"Opening vs image tracker crack length: mean {dd.mean():+.1f} px, "
                  f"RMS {np.sqrt(np.mean(dd ** 2)):.1f} px over {dd.size} frames")

    # --- plots ---
    xlab = "Time (s)" if have_time else "Frame"
    fig, ax = plt.subplots(1, 3, figsize=(18, 4.5))
    ax[0].plot(t, length, ".-", ms=2, lw=0.8, label="opening method")
    if img is not None:
        ax[0].plot(img["time_s"] if have_time else img["frame_index"],
                   img["crack_length_px"], ".-", ms=2, lw=0.8, label="image tracker")
    for c in crack_marks:
        j = np.argmin(np.abs(nums - c))
        ax[0].axvline(t[j], color="r", ls="--", lw=0.8)
    ax[0].set_xlabel(xlab)
    ax[0].set_ylabel("Crack length from notch (px)")
    ax[0].set_title("Crack length")
    ax[0].legend()

    if dense.size:
        w = np.arange(max(0, dense[0] - 2), min(n, dense[-1] + 3))
        ax[1].plot(t[w], length[w], ".-", ms=3, lw=0.8)
        ax2 = ax[1].twinx()
        ax2.plot(t[w], speed[w], color="C1", lw=0.8)
        ax2.set_ylabel("speed (px/s)", color="C1")
        ax[1].set_title("Crack window (full rate)")
    else:
        ax[1].set_title("no full-rate window found")
    ax[1].set_xlabel(xlab)

    pick = np.unique(np.linspace(dense[0] if dense.size else 0, n - 1, 6).astype(int))
    for k in pick:
        line, = ax[2].plot(gx_s, opening[k], label=f"{t[k]:.3f} s" if have_time else f"f{nums[k]}")
        if np.isfinite(tip_raw[k]):
            ax[2].axvline(tip_raw[k], color=line.get_color(), ls="--", lw=0.8)
    ax[2].axhline(thr, color="k", ls=":", lw=1, label="threshold")
    ax[2].set_xlabel("x (px)")
    ax[2].set_ylabel("opening (px)")
    ax[2].set_title("Opening profiles (dashed = tip)")
    ax[2].legend(fontsize=7)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
