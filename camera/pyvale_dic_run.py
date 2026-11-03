# -*- coding: utf-8 -*-
"""
pyvale_dic_run.py

Runs the Pyvale DIC engine on an image folder, computes strain, and plots
results. Works for any image set (stationary or deforming).

- Picks up .tif/.tiff/.png/.jpg/.bmp images automatically.
- FRAME_STEP lets you run on every Nth frame for a quick first pass.
- Results go to a separate output folder so runs don't mix.

Function/argument names checked against installed pyvale 2026.10.2.

Use:
    python3 pyvale_dic_run.py <run folder>   -> that run's images + DIC results;
                                                plots saved as PNG, not shown
                                                (capture_dic_live.py does this
                                                after each test)
    python3 pyvale_dic_run.py                -> image_folder / output_folder
                                                below, or the newest run

The vsbc GUI runs this file after each test with image_folder, output_folder,
RUN_DIC = False, MAKE_ANIMATION and ANIM_STEP set for that test (it rewrites
those lines); each plt.show() then saves the figures to <run>/plots/.
Changes made while porting it to the GUI are marked "FIX:".
"""

import os
import sys
import csv
import glob
import json
import time
from pathlib import Path

import numpy as np
import matplotlib

# A run folder on the command line: no windows, the plots are saved as PNG
AUTO_RUN = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
if AUTO_RUN:
    matplotlib.use("Agg")
import matplotlib.pyplot as plt

import pyvale.dic as dic
import pyvale.strain as strain


# ---------------------------------------------------------------------------
# USER SETTINGS
# ---------------------------------------------------------------------------
RUNS_ROOT = (r"C:\VSBC_DIC_Test\captures" if os.name == "nt"
             else os.path.expanduser("~/vsbc_captures"))
image_folder = None    # None = <run>/images of the newest run in RUNS_ROOT
output_folder = None   # None = <run>/pyvale_results

RUN_DIC = False        # False = use the DIC results capture_dic_live.py made
                       # True  = draw an ROI and redo DIC on image_folder
FRAME_STEP = 1         # 1 = every frame; 10 = every 10th frame (fast first pass)
MAX_FRAMES = None      # e.g. 100 to stop early; None = use all selected frames

subset_size = 41       # pixels (odd number); old pydic window was 80
subset_step = 20       # pixels; matches the old pydic grid spacing
max_displacement = 128 # pixels; must exceed the largest motion from frame 0

strain_window_size = 5
strain_window_element = 9
# Pyvale strain columns: eps_00 = xx, eps_01/eps_10 = xy, eps_11 = yy
# (0 = x / horizontal, 1 = y / vertical in the image)
STRAIN_COLUMN = "eps_11"

MAKE_ANIMATION = True  # saves strain_animation.mp4 (or .gif) in output_folder
ANIM_FPS = 30          # 900 frames at 30 fps = 30 s video
ANIM_STEP = 1          # 1 = every analyzed frame in the video; 3 = every 3rd
ANIM_RANGE = "crack"   # "crack" = only the full-rate crack window (slow motion);
                       # "all" = the whole test
STRAIN_CLIM = None     # e.g. (-0.01, 0.05) to fix the color range; None = auto

if AUTO_RUN:
    image_folder = os.path.join(AUTO_RUN, "images")
    output_folder = os.path.join(AUTO_RUN, "pyvale_results")
    RUN_DIC = False
elif image_folder is None:
    _runs = sorted(d for d in glob.glob(os.path.join(RUNS_ROOT, "*"))
                   if os.path.isdir(os.path.join(d, "images")))
    assert _runs, (f"No runs in {RUNS_ROOT}: give a run folder on the command "
                   "line or set image_folder")
    image_folder = os.path.join(_runs[-1], "images")
if output_folder is None:
    output_folder = os.path.join(os.path.dirname(os.path.normpath(image_folder)),
                                 "pyvale_results")
print(f"Images: {image_folder}\nResults: {output_folder}")


def _show(name):
    """Show the figures, or with a run folder on the command line save them as PNG."""
    if AUTO_RUN:
        path = os.path.join(output_folder, name + ".png")
        plt.savefig(path, dpi=120)
        plt.close()
        print(f"Saved {path}")
    else:
        plt.show()


# ---------------------------------------------------------------------------
# Find images
# ---------------------------------------------------------------------------
extensions = ("*.tif", "*.tiff", "*.png", "*.jpg", "*.jpeg", "*.bmp")
image_list = []
for ext in extensions:
    image_list.extend(glob.glob(os.path.join(image_folder, ext)))
image_list = sorted(set(image_list))

assert len(image_list) > 1, f"No images found in {image_folder}"

reference_image = image_list[0]
deformed_list = image_list[1::FRAME_STEP]
if MAX_FRAMES is not None:
    deformed_list = deformed_list[:MAX_FRAMES]
deformed_paths = [Path(p) for p in deformed_list]

print(f"Found {len(image_list)} images in {image_folder}")
print(f"File type: {os.path.splitext(reference_image)[1]}")
print(f"Reference image: {os.path.basename(reference_image)}")
print(f"Deformed images to analyze: {len(deformed_paths)} "
      f"(every {FRAME_STEP} frame(s))")

os.makedirs(output_folder, exist_ok=True)
output_basepath = output_folder + os.sep


# ---------------------------------------------------------------------------
# Region of interest: draw it interactively on the reference image
# ---------------------------------------------------------------------------
if RUN_DIC:
    roi = dic.RegionOfInterest(reference_image)
    roi.interactive_selection()

    # -----------------------------------------------------------------------
    # Run DIC (each deformed image is correlated against the reference image)
    # -----------------------------------------------------------------------
    print("\nRunning Pyvale DIC...")
    t0 = time.perf_counter()
    dic.calculate_2d(
        reference=reference_image,
        deformed=deformed_paths,
        roi_mask=roi.mask,
        seed=[int(round(v)) for v in np.asarray(roi.seed, float).reshape(-1, 2)[0]],
        subset_size=subset_size,
        subset_step=subset_step,
        max_displacement=max_displacement,
        output_basepath=output_basepath,
    )
    dic_time = time.perf_counter() - t0
    print(f"DIC complete in {dic_time:.1f} s "
          f"({dic_time / len(deformed_paths):.2f} s per frame)")
else:
    print("\nRUN_DIC = False: skipping ROI and DIC, loading existing results.")


# ---------------------------------------------------------------------------
# Load DIC results. Arrays are indexed [image, y, x]
# ---------------------------------------------------------------------------
dic_data = dic.import_2d(
    data=os.path.join(output_folder, "dic_results_*"),
    delimiter=",",
    binary=False,
)

# Pyvale names: u_px = horizontal, v_px = vertical displacement (pixels)
u_x = np.asarray(dic_data.u_px, dtype=float)
u_y = np.asarray(dic_data.v_px, dtype=float)
n_frames = u_x.shape[0]

# Camera frame number of each result (from the file name) and, if the run
# folder has a frames.csv from capture_dic_live.py, its time in seconds.
import re as _re
_result_files = sorted(glob.glob(os.path.join(output_folder, "dic_results_*")))
frame_numbers = np.array([int(_re.findall(r"(\d+)", os.path.basename(f))[-1])
                          for f in _result_files])
frame_time = None
_frames_csv = os.path.join(os.path.dirname(os.path.normpath(image_folder)),
                           "frames.csv")
if os.path.exists(_frames_csv) and len(frame_numbers) == n_frames:
    # FIX: read with the csv module. np.genfromtxt(dtype=None) guesses each
    # column's type from its contents, which goes wrong when "marker" is empty.
    with open(_frames_csv, newline="") as _fh:
        _rows = list(csv.DictReader(_fh))
    _ts = {int(r["frame_index"]): int(r["camera_timestamp_ns"]) for r in _rows}
    _tt = {int(r["frame_index"]): float(r["time_s"]) for r in _rows if r.get("time_s")}
    if _tt and all(k in _tt for k in frame_numbers):
        # Recorded by the vsbc GUI: the same clock and zero as the test's data.csv
        frame_time = np.array([_tt[k] for k in frame_numbers])
    elif all(k in _ts for k in frame_numbers) and min(_ts.values()) >= 0:
        _t0 = _ts[min(_ts)]
        frame_time = np.array([(_ts[k] - _t0) * 1e-9 for k in frame_numbers])
    _crack_rows = [int(r["frame_index"]) for r in _rows
                   if (r.get("marker") or "").startswith("crack")]
else:
    _crack_rows = []

# Exclude subsets that did not converge
converged = np.asarray(dic_data.converged).astype(bool)
u_x = np.where(converged, u_x, np.nan)
u_y = np.where(converged, u_y, np.nan)

mag = np.sqrt(u_x**2 + u_y**2)
mean_mag = np.nanmean(mag, axis=(1, 2))
max_mag = np.nanmax(mag, axis=(1, 2))
valid_frac = np.mean(converged, axis=(1, 2))

print("\n=== Displacement summary (pixels, relative to reference frame) ===")
print(f"Frames analyzed:               {n_frames}")
print(f"Mean |u| in last frame:        {mean_mag[-1]:.4f}")
print(f"Max |u| in any frame:          {np.nanmax(max_mag):.4f}")
print(f"Lowest fraction of valid pts:  {np.min(valid_frac):.3f}")
if np.nanmax(max_mag) > 0.8 * max_displacement:
    print("WARNING: displacements are close to max_displacement; "
          "increase max_displacement and rerun.")


# ---------------------------------------------------------------------------
# Strain from the DIC displacement results
# ---------------------------------------------------------------------------
print("\nCalculating strain...")
t0 = time.perf_counter()
strain.calculate_2d(
    data=os.path.join(output_folder, "dic_results_*"),
    window_size=strain_window_size,
    window_element=strain_window_element,
    input_binary=False,
    input_delimiter=",",
    output_basepath=output_basepath,
)
print(f"Strain complete in {time.perf_counter() - t0:.1f} s")

# pyvale's strain.import_2d requires every frame to have the same set of
# strain points. Points whose DIC subsets fail to converge are dropped from
# that frame's file, so later frames have fewer points and import_2d fails.
# Instead, read each CSV ourselves and place it on one common grid, with
# NaN wherever a frame has no value.
strain_files = sorted(glob.glob(os.path.join(output_folder, "strain_*.csv")))
assert len(strain_files) > 0, "No strain_*.csv files found"
if len(strain_files) != n_frames:
    # FIX: say what is wrong before the plots and the animation fail on it
    print(f"WARNING: {len(strain_files)} strain files but {n_frames} DIC results in "
          f"{output_folder}: strain_*.csv files from an earlier run are mixed in. "
          "Delete them and run again.")

with open(strain_files[0], "r") as fh:
    strain_columns = [c.strip().strip('"').strip("'")
                      for c in fh.readline().strip().split(",")]
print(f"Strain file columns: {strain_columns}")

frames_arr = [np.loadtxt(f, delimiter=",", skiprows=1, ndmin=2)
              for f in strain_files]

# First two columns are the strain-window x and y pixel coordinates
strain_x = np.unique(np.concatenate([a[:, 0] for a in frames_arr]))
strain_y = np.unique(np.concatenate([a[:, 1] for a in frames_arr]))

def _to_grid(a, col_idx):
    grid = np.full((strain_y.size, strain_x.size), np.nan)
    ix = np.searchsorted(strain_x, a[:, 0])
    iy = np.searchsorted(strain_y, a[:, 1])
    grid[iy, ix] = a[:, col_idx]
    return grid

# Pick the yy strain column by name
eyy_col = None
if STRAIN_COLUMN is not None:
    assert STRAIN_COLUMN in strain_columns, \
        f"STRAIN_COLUMN '{STRAIN_COLUMN}' not in {strain_columns}"
    eyy_col, eyy_idx = STRAIN_COLUMN, strain_columns.index(STRAIN_COLUMN)
for i, c in enumerate(strain_columns if eyy_col is None else []):
    lc = c.lower()
    if i >= 2 and "yy" in lc and not lc.startswith("def_grad"):
        eyy_col, eyy_idx = c, i
        break

points_per_frame = np.array([a.shape[0] for a in frames_arr])
print(f"Strain points per frame: {points_per_frame.min()} to "
      f"{points_per_frame.max()} (full grid = {strain_x.size * strain_y.size})")

if eyy_col is not None:
    eps_yy = np.stack([_to_grid(a, eyy_idx) for a in frames_arr])
    p02 = np.nanpercentile(eps_yy, 2)
    p98 = np.nanpercentile(eps_yy, 98)
    print(f"Using strain column '{eyy_col}'")
    print(f"eps_yy 2nd-98th percentile (all frames): {p02:.6f} to {p98:.6f}")
else:
    eps_yy = None
    print("Could not find a yy strain column; strain plot skipped. "
          "Send the 'Strain file columns' line above.")


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------
last = n_frames - 1

fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))

_x = frame_time if frame_time is not None else frame_numbers
axes[0].plot(_x, mean_mag, ".-", ms=2, lw=0.8, label="mean |u|")
axes[0].plot(_x, max_mag, ".-", ms=2, lw=0.8, label="max |u|")
if frame_time is not None:
    _lookup = dict(zip(frame_numbers, frame_time))
    for _c in _crack_rows:
        _near = frame_numbers[np.argmin(np.abs(frame_numbers - _c))]
        axes[0].axvline(_lookup[_near], color="r", ls="--", lw=0.8)
axes[0].set_xlabel("Time (s)" if frame_time is not None else "Camera frame number")
axes[0].set_ylabel("Displacement (px)")
axes[0].set_title("Displacement vs. frame")
axes[0].legend()

im1 = axes[1].pcolor(dic_data.ss_x, dic_data.ss_y, u_y[last], shading="auto")
axes[1].set_title(f"u_y, last analyzed frame (px)")
axes[1].invert_yaxis()
fig.colorbar(im1, ax=axes[1])

if eps_yy is not None:
    im2 = axes[2].pcolor(
        strain_x,
        strain_y,
        eps_yy[-1],
        vmin=p02,
        vmax=p98,
        shading="auto",
    )
    axes[2].set_title("eps_yy, last analyzed frame")
    axes[2].invert_yaxis()
    fig.colorbar(im2, ax=axes[2])
else:
    axes[2].set_axis_off()
    axes[2].set_title("strain field names not found")

plt.tight_layout()
_show("summary_plots")


# ---------------------------------------------------------------------------
# Whole-field motion diagnostics (most useful on a STILL specimen):
# mean displacement and mean strain vs time. On a still specimen a
# field-wide mean strain that swings with time means the image is growing /
# shrinking: the camera-to-specimen distance is changing (out-of-plane
# vibration), which a 6 mm lens this close is very sensitive to.
# ---------------------------------------------------------------------------
try:
    _exx_idx = strain_columns.index("eps_00")
    _exx = np.stack([_to_grid(a, _exx_idx) for a in frames_arr])
except (ValueError, NameError):
    _exx = None
_xt = frame_time if frame_time is not None else frame_numbers
figD, axD = plt.subplots(1, 2, figsize=(14, 4))
axD[0].plot(_xt, np.nanmean(u_x, axis=(1, 2)), ".-", ms=2, lw=0.8, label="mean u (x)")
axD[0].plot(_xt, np.nanmean(u_y, axis=(1, 2)), ".-", ms=2, lw=0.8, label="mean v (y)")
axD[0].set_ylabel("px")
axD[0].set_title("Whole-field mean displacement")
axD[0].legend()
if eps_yy is not None and len(eps_yy) == len(_xt):
    axD[1].plot(_xt, 1e6 * np.nanmean(eps_yy, axis=(1, 2)), ".-", ms=2, lw=0.8,
                label=f"mean {eyy_col}")
    if _exx is not None:
        axD[1].plot(_xt, 1e6 * np.nanmean(_exx, axis=(1, 2)), ".-", ms=2,
                    lw=0.8, label="mean eps_00 (xx)")
    axD[1].set_ylabel("microstrain")
    axD[1].set_title("Whole-field mean strain")
    axD[1].legend()
for a in axD:
    a.set_xlabel("Time (s)" if frame_time is not None else "Camera frame number")
plt.tight_layout()
_show("motion_diagnostics")


# ---------------------------------------------------------------------------
# Strain overlaid on the specimen
# Strain points are in reference-image pixel coordinates, so they are drawn
# on top of the reference image.
# ---------------------------------------------------------------------------
if eps_yy is not None:
    ref_img = plt.imread(reference_image)
    fig2, ax2 = plt.subplots(figsize=(12, 6))
    ax2.imshow(ref_img, cmap="gray")
    im3 = ax2.pcolormesh(strain_x, strain_y, eps_yy[-1],
                         vmin=p02, vmax=p98, cmap="jet",
                         alpha=0.55, shading="auto")
    fig2.colorbar(im3, ax=ax2, label=f"{eyy_col} (last analyzed frame)")
    pad = 60
    ax2.set_xlim(strain_x.min() - pad, strain_x.max() + pad)
    ax2.set_ylim(strain_y.max() + pad, strain_y.min() - pad)
    ax2.set_title(f"{eyy_col} on specimen, frame "
                  f"{os.path.basename(strain_files[-1])}")
    plt.tight_layout()
    _show("strain_on_specimen")


# ---------------------------------------------------------------------------
# Animation: strain on each deformed frame as the crack grows
#
# Strain points are calculated at reference-image positions. To keep the
# color map registered to the crack in each deformed image, each point is
# moved by its measured DIC displacement (interpolated from the subset grid).
# ---------------------------------------------------------------------------
if MAKE_ANIMATION and eps_yy is not None:
    from matplotlib.animation import FuncAnimation, PillowWriter
    from scipy.interpolate import griddata

    n_anim = len(strain_files)
    assert u_x.shape[0] == n_anim, (
        f"{u_x.shape[0]} DIC frames vs {n_anim} strain frames; "
        "delete old result files and rerun")

    # Image file for each strain frame, from its name:
    # strain_dic_results_frame_0601.csv -> frame_0601.tif
    anim_images = []
    for f in strain_files:
        stem = os.path.splitext(os.path.basename(f))[0]
        stem = stem.replace("strain_", "", 1).replace("dic_results_", "", 1)
        matches = glob.glob(os.path.join(image_folder, stem + ".*"))
        assert matches, f"No image found for {stem} in {image_folder}"
        anim_images.append(matches[0])

    # DIC subset positions (flattened) for interpolating displacement
    ssx = np.asarray(dic_data.ss_x, dtype=float)
    ssy = np.asarray(dic_data.ss_y, dtype=float)
    if ssx.ndim == 1:
        ssx, ssy = np.meshgrid(ssx, ssy)
    ss_pts = np.column_stack([ssx.ravel(), ssy.ravel()])
    SX, SY = np.meshgrid(strain_x, strain_y)

    def _shift(k):
        """Strain-point positions in deformed frame k."""
        ux = u_x[k].ravel()
        uy = u_y[k].ravel()
        ok = np.isfinite(ux) & np.isfinite(uy)
        if ok.sum() < 4:
            return SX, SY
        dx = griddata(ss_pts[ok], ux[ok], (SX, SY), method="linear")
        dy = griddata(ss_pts[ok], uy[ok], (SX, SY), method="linear")
        dx_n = griddata(ss_pts[ok], ux[ok], (SX, SY), method="nearest")
        dy_n = griddata(ss_pts[ok], uy[ok], (SX, SY), method="nearest")
        dx = np.where(np.isfinite(dx), dx, dx_n)
        dy = np.where(np.isfinite(dy), dy, dy_n)
        return SX + dx, SY + dy

    vmin, vmax = STRAIN_CLIM if STRAIN_CLIM is not None else (p02, p98)
    pad = 60
    xlim = (strain_x.min() - pad, strain_x.max() + pad)
    ylim = (strain_y.max() + pad, strain_y.min() - pad)

    figA, axA = plt.subplots(figsize=(10, 4))
    cbar_im = None

    def _draw(k):
        global cbar_im
        axA.clear()
        axA.imshow(plt.imread(anim_images[k]), cmap="gray")
        X, Y = _shift(k)
        # Grid cells can fold slightly where the crack opens; that is expected
        import warnings
        warnings.filterwarnings("ignore", message=".*monotonically.*")
        m = axA.pcolormesh(X, Y, np.ma.masked_invalid(eps_yy[k]),
                           vmin=vmin, vmax=vmax, cmap="jet",
                           alpha=0.55, shading="nearest")
        axA.set_xlim(*xlim)
        axA.set_ylim(*ylim)
        axA.set_title(f"{eyy_col}  |  {os.path.basename(anim_images[k])}  "
                      f"({k + 1}/{n_anim})")
        axA.set_axis_off()
        if cbar_im is None:
            cbar_im = figA.colorbar(m, ax=axA, label=eyy_col)
        return []

    print(f"\nBuilding animation ({n_anim} frames)...")
    t0 = time.perf_counter()
    from matplotlib.animation import writers, FFMpegWriter
    # Full-rate windows: capture_settings.json (crack_windows from
    # capture_dic_live.py, full_rate_windows from the vsbc GUI), else runs of
    # consecutive frame numbers.
    _win = []
    _cs = os.path.join(os.path.dirname(os.path.normpath(image_folder)),
                       "capture_settings.json")
    if os.path.exists(_cs):
        with open(_cs) as _fh:
            _meta = json.load(_fh)
        _win = _meta.get("full_rate_windows") or _meta.get("crack_windows") or []
    if _win:
        _in = np.zeros(n_anim, bool)
        for _a, _b in _win:
            _in |= (frame_numbers >= _a) & (frame_numbers <= _b)
    else:
        _in = np.zeros(n_anim, bool)
        _d = np.diff(frame_numbers) == 1
        _in[:-1] |= _d
        _in[1:] |= _d
    if ANIM_RANGE == "crack" and _in.any():
        _i0 = int(np.argmax(_in))
        _i1 = n_anim - 1 - int(np.argmax(_in[::-1]))
        anim_frames = list(range(_i0, _i1 + 1, ANIM_STEP))
        print(f"Animating the crack window: frames {frame_numbers[_i0]}-{frame_numbers[_i1]}")
    else:
        anim_frames = list(range(0, n_anim, ANIM_STEP))
        if anim_frames[-1] != n_anim - 1:
            anim_frames.append(n_anim - 1)   # always end on the last frame
    anim = FuncAnimation(figA, _draw, frames=anim_frames, blit=False)

    # MP4 if ffmpeg is installed (much smaller file); otherwise GIF
    if writers.is_available("ffmpeg"):
        anim_path = os.path.join(output_folder, "strain_animation.mp4")
        anim.save(anim_path, writer=FFMpegWriter(fps=ANIM_FPS, bitrate=4000),
                  dpi=110)
    else:
        print("ffmpeg not found, saving GIF instead "
              "(for MP4: 'sudo apt install ffmpeg' on the Pi, "
              "'mamba install ffmpeg' in the laptop's dic environment)")
        anim_path = os.path.join(output_folder, "strain_animation.gif")
        anim.save(anim_path, writer=PillowWriter(fps=ANIM_FPS), dpi=80)
    plt.close(figA)
    print(f"Saved {anim_path} ({len(anim_frames)} frames) in "
          f"{time.perf_counter() - t0:.1f} s")
