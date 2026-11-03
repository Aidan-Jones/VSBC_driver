# -*- coding: utf-8 -*-
"""
crack_tip_image.py

Image-based crack-tip tracker for runs recorded with capture_dic_live.py.

Same method as crack_tip_auto_motion.py / crack_tip_auto_pyvale.py
(reference warped with DIC motion -> reference-gated crack score ->
notch-connected dynamic-programming path with short weak-gap bridging ->
sustained-low endpoint -> jump validation -> no-backward rule), with:

  1. FASTER path search: the per-pixel Python loop is replaced by NumPy
     array operations over each column. Same rule, same tie-breaking;
     identical results (see verify_dp_equivalence()).
  2. NOTCH position from settings (no manual CSV needed). A manual CSV can
     still be used for the start and for comparison.
  3. JUMP LIMITS from crack speed and the real time between analyzed
     frames, so a fast brittle crack is not rejected as a "false jump".
     LEGACY_JUMP_RULES = True restores the old fixed 5 / 18 px per frame.
  4. Real time (s) from frames.csv; crack direction can be left->right or
     right->left.

Uses the frames that have DIC results (whole-test overview + dense crack
window). Output: <run>/crack_tip_image.csv and plots.

The vsbc GUI runs this file after each test with RUN_FOLDER, NOTCH_TIP and
CRACK_FROM_LEFT set for that test (it rewrites those lines). Changes made
while porting it to the GUI are marked "FIX:".
"""

import os
import re
import csv
import glob
import time

import numpy as np
import cv2
import matplotlib.pyplot as plt
from scipy.interpolate import griddata


# ---------------------------------------------------------------------------
# USER SETTINGS
# ---------------------------------------------------------------------------
RUN_FOLDER = r"C:\VSBC_DIC_Test\captures\YYYYMMDD_HHMMSS_test01"

# Where the crack starts (notch tip), in pixels of the RECORDED images
# (cropped band coordinates). Find it in roi_snapshot.tif or frame_000000.tif.
NOTCH_TIP = None            # e.g. (205, 170); None = use MANUAL_CSV first row
MANUAL_CSV = None           # optional: manual tip CSV (frame filename/index, x, y)
CRACK_FROM_LEFT = True      # False if the notch is on the right

# --- crack score (unchanged from the tuned tracker) ---
corridor_half_height = 35
difference_weight = 0.90
darkness_weight = 0.10
reference_dark_cutoff = 0.15

# --- notch-connected path (unchanged) ---
max_vertical_step_per_column = 2
vertical_penalty = 0.05
weak_score_threshold = 0.16
max_weak_run = 10

# --- endpoint (unchanged) ---
stop_score_threshold = 0.20
stop_low_run_length = 10
tip_smoothing_window = 5

# --- frame-to-frame checks ---
LEGACY_JUMP_RULES = False   # True = old fixed limits (5 / 18 px per frame)
MAX_CRACK_SPEED_PX_S = 50000.0   # fastest believable crack speed (px/s);
                                 # 50,000 px/s ~ crossing 1,600 px in 32 ms
NORMAL_STEP_PX = 5          # always accepted (if vertical motion is OK)
max_vertical_step_frame = 15
strong_jump_mean_score = 0.24
strong_jump_fraction_threshold = 0.65

extend_margin_px = 5


# ---------------------------------------------------------------------------
# Fast notch-connected path (vectorized over rows, loop over columns)
# ---------------------------------------------------------------------------
def notch_path_dp(path_image, local_start_y):
    """Same rule as the original nested loops:
    for each column, each row takes the best of up to 5 predecessor rows
    (|dy| <= max_vertical_step_per_column), score = prev + pixel -
    vertical_penalty*|dy|, skipping predecessors that are invalid or whose
    weak run would exceed max_weak_run. Ties -> lowest predecessor row
    (the original loop's strict '>' kept the first one found)."""
    pim = np.asarray(path_image, dtype=float)
    height, width = pim.shape
    S = max_vertical_step_per_column
    nd = 2 * S + 1
    scores = np.full((height, width), -np.inf)
    back = np.full((height, width), -1, dtype=np.int32)
    weak = np.full((height, width), 9999, dtype=np.int32)

    y0, y1 = max(0, local_start_y - 3), min(height, local_start_y + 4)
    scores[y0:y1, 0] = pim[y0:y1, 0] - vertical_penalty * np.abs(np.arange(y0, y1) - local_start_y)
    weak[y0:y1, 0] = np.where(pim[y0:y1, 0] < weak_score_threshold, 1, 0)

    pen = vertical_penalty * np.abs(np.arange(-S, S + 1))[:, None]   # (nd,1)
    rows = np.arange(height)
    prev_rows = rows[None, :] + np.arange(-S, S + 1)[:, None]        # (nd,h)
    in_range = (prev_rows >= 0) & (prev_rows < height)
    pr = np.clip(prev_rows, 0, height - 1)
    is_weak_all = pim < weak_score_threshold
    cand = np.empty((nd, height))
    for xx in range(1, width):
        ps = scores[pr, xx - 1]                       # (nd,h)
        pw = weak[pr, xx - 1]
        nw = np.where(is_weak_all[:, xx], pw + 1, 0)
        valid = in_range & np.isfinite(ps) & (nw <= max_weak_run)
        np.add(ps, pim[:, xx], out=cand)      # same order as the original:
        cand -= pen                            # (prev + pixel) - penalty
        cand[~valid] = -np.inf
        best = cand.argmax(axis=0)                    # first max = lowest prev row
        bs = cand[best, rows]
        good = np.isfinite(bs)
        scores[:, xx] = bs
        back[:, xx] = np.where(good, pr[best, rows], -1)
        weak[:, xx] = np.where(good, nw[best, rows], 9999)
    return scores, back, weak


def notch_path_dp_loop(path_image, local_start_y):
    """The ORIGINAL nested-loop version, kept only to verify equivalence."""
    height, width = path_image.shape
    scores = np.full((height, width), -np.inf)
    back = np.full((height, width), -1, dtype=int)
    weak = np.full((height, width), 9999, dtype=int)
    for yy in range(max(0, local_start_y - 3), min(height, local_start_y + 4)):
        ls = float(path_image[yy, 0])
        scores[yy, 0] = ls - vertical_penalty * abs(yy - local_start_y)
        weak[yy, 0] = 1 if ls < weak_score_threshold else 0
    for xx in range(1, width):
        for yy in range(height):
            pix = float(path_image[yy, xx])
            best, bp, bw = -np.inf, -1, 9999
            for py in range(max(0, yy - max_vertical_step_per_column),
                            min(height - 1, yy + max_vertical_step_per_column) + 1):
                ps = scores[py, xx - 1]
                if not np.isfinite(ps):
                    continue
                nw = weak[py, xx - 1] + 1 if pix < weak_score_threshold else 0
                if nw > max_weak_run:
                    continue
                c = ps + pix - vertical_penalty * abs(yy - py)
                if c > best:
                    best, bp, bw = c, py, nw
            scores[yy, xx], back[yy, xx], weak[yy, xx] = best, bp, bw
    return scores, back, weak


def verify_dp_equivalence(trials=5, seed=0):
    rng = np.random.default_rng(seed)
    for t in range(trials):
        img = rng.random((31, 60)).astype(np.float32) * 0.4
        img[15, :40] += 0.5
        a = notch_path_dp(img, 15)
        b = notch_path_dp_loop(img, 15)
        for x, y in zip(a, b):
            if not np.array_equal(np.nan_to_num(x, neginf=-1e9),
                                  np.nan_to_num(y, neginf=-1e9)):
                return False
    return True


# ---------------------------------------------------------------------------
# Endpoint from the best path (unchanged logic)
# ---------------------------------------------------------------------------
def endpoint(path_image, scores, back):
    valid_cols = np.where(np.isfinite(scores).any(axis=0))[0]
    if valid_cols.size == 0:
        return None
    final_col = int(valid_cols.max())
    path_y = np.zeros(final_col + 1, dtype=int)
    path_y[final_col] = int(np.argmax(scores[:, final_col]))
    for xx in range(final_col, 0, -1):
        py = back[path_y[xx], xx]
        path_y[xx - 1] = path_y[xx] if py < 0 else py
    path_scores = path_image[path_y, np.arange(final_col + 1)].astype(float)
    if tip_smoothing_window > 1:
        k = np.ones(tip_smoothing_window) / tip_smoothing_window
        sm = np.convolve(path_scores, k, mode="same")
    else:
        sm = path_scores.copy()
    low_run, cand = 0, 0
    for xx in range(len(sm)):
        if sm[xx] >= stop_score_threshold:
            low_run, cand = 0, xx
        else:
            low_run += 1
            if low_run >= stop_low_run_length:
                cand = max(0, xx - low_run)
                break
    return cand, path_y, sm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def frame_num(name):
    m = re.search(r"frame[_\-]?(\d+)", str(name))
    return int(m.group(1)) if m else None


def frame_times(run_folder):
    """{frame: time in s} from frames.csv. FIX: uses the time_s column written by
    the vsbc GUI (the same clock and zero as the test's data.csv) when it is
    there, else the camera clock counted from frame 0 as before."""
    path = os.path.join(run_folder, "frames.csv")
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if rows and rows[0].get("time_s") not in (None, ""):
        return {int(r["frame_index"]): float(r["time_s"]) for r in rows
                if r.get("time_s") not in (None, "")}
    ts = {int(r["frame_index"]): int(r["camera_timestamp_ns"]) for r in rows}
    if not ts or min(ts.values()) < 0:
        return {}
    t0 = ts[min(ts)]
    return {k: (v - t0) * 1e-9 for k, v in ts.items()}


def load_manual(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path) as fh:
        hdr = [h.strip().strip('"').lower() for h in fh.readline().split(",")]
    data = np.genfromtxt(path, delimiter=",", skip_header=1, dtype=str, ndmin=2)
    xi = next(i for i, h in enumerate(hdr) if "x" in h and "tip" in h)
    yi = next((i for i, h in enumerate(hdr) if "y" in h and "tip" in h), None)
    fi = next((i for i, h in enumerate(hdr) if "file" in h), None)
    ii = next((i for i, h in enumerate(hdr) if "frame" in h and "index" in h), None)
    out = {}
    for row in data:
        n = frame_num(row[fi]) if fi is not None else int(float(row[ii]))
        out[n] = (float(row[xi]), float(row[yi]) if yi is not None else np.nan)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import pyvale.dic as dic

    assert verify_dp_equivalence(), "fast path search differs from original"

    images = os.path.join(RUN_FOLDER, "images")
    results = os.path.join(RUN_FOLDER, "pyvale_results")
    res_files = sorted(glob.glob(os.path.join(results, "dic_results_*")))
    assert res_files, f"No DIC results in {results}"
    nums = [frame_num(os.path.basename(f)) for f in res_files]

    print("Loading DIC results...")
    d = dic.import_2d(data=os.path.join(results, "dic_results_*"),
                      delimiter=",", binary=False)
    U = np.asarray(d.u_px, float)
    V = np.asarray(d.v_px, float)
    C = np.asarray(d.converged).astype(bool)
    ssx = np.asarray(d.ss_x, float)
    ssy = np.asarray(d.ss_y, float)
    if ssx.ndim == 1:
        ssx, ssy = np.meshgrid(ssx, ssy)
    assert U.shape[0] == len(nums)

    # Time per frame from frames.csv
    t_of = frame_times(RUN_FOLDER)
    have_time = all(n in t_of for n in nums) and 0 in t_of

    manual = load_manual(MANUAL_CSV)
    if NOTCH_TIP is not None:
        start_x, start_y = map(float, NOTCH_TIP)
    elif manual:
        start_x, start_y = manual[min(manual)]
    else:
        raise ValueError("Set NOTCH_TIP = (x, y) or MANUAL_CSV")
    sgn = 1 if CRACK_FROM_LEFT else -1
    print(f"Start (notch tip): x={start_x:.0f}, y={start_y:.0f}, "
          f"crack grows {'right' if sgn > 0 else 'left'}")

    ref = cv2.imread(os.path.join(images, "frame_000000.tif"), cv2.IMREAD_GRAYSCALE)
    H, W = ref.shape
    y1 = int(max(start_y - corridor_half_height, 0))
    y2 = int(min(start_y + corridor_half_height, H - 1))
    if sgn > 0:
        x1 = int(max(min(np.nanmin(ssx), start_x - extend_margin_px), 0))
        x2 = int(min(np.nanmax(ssx), W - 1))
    else:
        x1 = int(max(np.nanmin(ssx), 0))
        x2 = int(min(max(np.nanmax(ssx), start_x + extend_margin_px), W - 1))
    xs = np.arange(x1, x2 + 1)
    ys = np.arange(y1, y2 + 1)
    X, Y = np.meshgrid(xs, ys)
    lsx = int(round(start_x - x1))
    lsy = int(round(start_y - y1))
    ref_c = ref.astype(np.float32)

    prev_x, prev_y = start_x, start_y
    prev_t = t_of[0] if have_time else 0.0
    # FIX: the first row's crack length was start_x (the notch's x position)
    # instead of 0, so the crack-length plot started with a spike.
    out = [(0, t_of[0] if have_time else np.nan, start_x, start_y, 0.0)]
    t_run = time.perf_counter()

    for k, n in enumerate(nums):
        cur = cv2.imread(os.path.join(images, f"frame_{n:06d}.tif"),
                         cv2.IMREAD_GRAYSCALE)
        if cur is None:
            continue
        t_now = t_of.get(n, np.nan)
        ok = C[k].ravel() & np.isfinite(U[k].ravel()) & np.isfinite(V[k].ravel())
        pts = np.column_stack((ssx.ravel()[ok], ssy.ravel()[ok]))
        u, v = U[k].ravel()[ok], V[k].ravel()[ok]
        try:
            Ui = griddata(pts, u, (X, Y), method="linear")
            Vi = griddata(pts, v, (X, Y), method="linear")
            Ui = np.where(np.isfinite(Ui), Ui, griddata(pts, u, (X, Y), method="nearest"))
            Vi = np.where(np.isfinite(Vi), Vi, griddata(pts, v, (X, Y), method="nearest"))
        except Exception:
            # FIX: fewer than 3 (or only collinear) converged points, e.g. right
            # after full separation: griddata raised and ended the whole run.
            # Keep the previous tip for this frame.
            out.append((n, t_now, prev_x, prev_y, abs(prev_x - start_x)))
            continue
        warped = cv2.remap(ref_c, (X - Ui).astype(np.float32),
                           (Y - Vi).astype(np.float32), cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        corr = cur[y1:y2 + 1, x1:x2 + 1].astype(np.float32)
        diff = np.abs(warped - corr) / 255.0
        dark = 1.0 - corr / 255.0
        gate = np.clip((warped / 255.0 - reference_dark_cutoff)
                       / (1.0 - reference_dark_cutoff), 0.0, 1.0)
        score = (difference_weight * diff * gate + darkness_weight * dark * gate)
        score = cv2.GaussianBlur(score.astype(np.float32), (3, 3), 0)

        # Path image starts at the notch and runs in the crack direction
        lx = int(np.clip(lsx, 0, score.shape[1] - 1))
        ly = int(np.clip(lsy, 0, score.shape[0] - 1))
        path_img = score[:, lx:] if sgn > 0 else score[:, :lx + 1][:, ::-1]

        scores, back, _ = notch_path_dp(path_img, ly)
        ep = endpoint(path_img, scores, back)
        new_x, new_y = prev_x, prev_y
        if ep is not None:
            cand_col, path_y, sm = ep
            cand_x = float(x1 + lx + sgn * cand_col)
            cand_y = float(y1 + path_y[cand_col])
            dx = sgn * (cand_x - prev_x)          # + = forward
            dy = cand_y - prev_y
            prev_col = int(np.clip(round(sgn * (prev_x - x1 - lx)), 0, len(sm) - 1))
            cc = int(np.clip(cand_col, 0, len(sm) - 1))
            seg = sm[prev_col:cc + 1] if cc >= prev_col else np.array([])
            jm = float(seg.mean()) if seg.size else 0.0
            jf = float(np.mean(seg >= stop_score_threshold)) if seg.size else 0.0

            if LEGACY_JUMP_RULES or not have_time:
                max_step = 18.0
            else:
                max_step = max(18.0, MAX_CRACK_SPEED_PX_S * max(t_now - prev_t, 0))
            if dx < 0:
                pass
            elif dx <= NORMAL_STEP_PX and abs(dy) <= max_vertical_step_frame:
                new_x, new_y = cand_x, cand_y
            elif dx <= max_step and abs(dy) <= max_vertical_step_frame:
                if jm >= strong_jump_mean_score and jf >= strong_jump_fraction_threshold:
                    new_x, new_y = cand_x, cand_y
        if sgn * (new_x - prev_x) < 0:
            new_x, new_y = prev_x, prev_y
        crack_len = abs(new_x - start_x)
        out.append((n, t_now, new_x, new_y, crack_len))
        prev_x, prev_y, prev_t = new_x, new_y, (t_now if np.isfinite(t_now) else prev_t)

        if (k + 1) % 100 == 0:
            el = time.perf_counter() - t_run
            print(f"  {k + 1}/{len(nums)} frames, tip x {new_x:.0f}, "
                  f"{el / (k + 1):.3f} s/frame")

    el = time.perf_counter() - t_run
    print(f"Done: {len(nums)} frames in {el:.1f} s ({el / max(1, len(nums)):.3f} s/frame)")

    out_csv = os.path.join(RUN_FOLDER, "crack_tip_image.csv")
    with open(out_csv, "w") as fh:
        fh.write("frame_index,time_s,crack_tip_x_px,crack_tip_y_px,crack_length_px\n")
        for r in out:
            fh.write(f"{r[0]},{r[1]:.6f},{r[2]:.2f},{r[3]:.2f},{r[4]:.2f}\n")
    print(f"Saved {out_csv}")

    # --- plots ---
    arr = np.array(out, dtype=float)
    xax = arr[:, 1] if have_time else arr[:, 0]
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.5))
    ax[0].plot(xax, arr[:, 4], ".-", ms=2, lw=0.8, label="image tracker")
    if manual:
        mf = sorted(manual)
        mx = [abs(manual[f][0] - start_x) for f in mf]
        mt = [t_of.get(f, np.nan) if have_time else f for f in mf]
        ax[0].plot(mt, mx, "o", ms=4, label="manual")
    ax[0].set_xlabel("Time (s)" if have_time else "Frame")
    ax[0].set_ylabel("Crack length from notch (px)")
    ax[0].set_title("Crack length")
    ax[0].legend()
    last = cv2.imread(os.path.join(images, f"frame_{int(arr[-1, 0]):06d}.tif"),
                      cv2.IMREAD_GRAYSCALE)
    ax[1].imshow(last, cmap="gray")
    ax[1].plot([start_x], [start_y], "c^", ms=8, label="notch")
    ax[1].plot([arr[-1, 2]], [arr[-1, 3]], "r+", ms=16, mew=2, label="tip (last)")
    ax[1].set_title(f"Last frame {int(arr[-1, 0])}")
    ax[1].legend()
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
