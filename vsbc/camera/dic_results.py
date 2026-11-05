"""The DIC tab's data: pyvale's displacement and strain files, crack-tip
lengths, crack tips marked by hand, and the checks before DIC runs.

pyvale writes one CSV per frame:
    pyvale_results/dic_results_frame_NNNNNN.csv          ss_x, ss_y, u, v, m, conv, ... (by position)
    pyvale_results/strain_dic_results_frame_NNNNNN.csv   x, y, eps_00, eps_01, eps_10, eps_11, ...
Frame numbers are read with the same rule as crack_tip_viewer.py. Tips marked
by hand go to camera/crack_tip_manual.csv in crack_tip_viewer.py's format, so
either tool can add to them and crack_tip_image.py can use them (MANUAL_CSV).

NumPy and the standard library only: runs in the GUI's environment.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vsbc.camera.jobs import dic_selection
from vsbc.camera.results import RunResults
from vsbc.camera.roi import Roi
from vsbc.camera.scripts import CAPTURE, CRACK_IMAGE, CRACK_OPENING, DIC_RUN, ScriptCheck
from vsbc.camera.sync import _column, _read_table

FRAME_NUMBER = re.compile(r"frame[_\-]?(\d+)")
MANUAL_FILE = "crack_tip_manual.csv"
MANUAL_HEADER = "frame_index,filename,crack_tip_x_px,crack_tip_y_px,time_s"
DISPLACEMENTS = {"v": "v: vertical displacement (px)", "u": "u: horizontal displacement (px)",
                 "mag": "|displacement| (px)"}
STRAIN_NAMES = {"eps_11": "eps_yy (vertical)", "eps_00": "eps_xx (horizontal)", "eps_01": "eps_xy (shear)",
                "eps_10": "eps_yx (shear)"}
DEFAULT_STRAIN = "eps_11"         # the scripts' STRAIN_COLUMN


# ------------------------------------------------------------------- files --

def frame_number(path: Path) -> int:
    m = FRAME_NUMBER.search(Path(path).name)
    return int(m.group(1)) if m else -1


def _numbered(folder: Path, pattern: str) -> dict[int, Path]:
    if not folder.is_dir():
        return {}
    out = {frame_number(p): p for p in folder.glob(pattern)}
    out.pop(-1, None)
    return dict(sorted(out.items()))


def dic_files(run: Path) -> dict[int, Path]:
    """frame index -> pyvale's displacement CSV."""
    return _numbered(Path(run) / "pyvale_results", "dic_results_*.csv")


def strain_files(run: Path) -> dict[int, Path]:
    """frame index -> pyvale's strain CSV."""
    return _numbered(Path(run) / "pyvale_results", "strain_*.csv")


def csv_header(path: Path) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [c.strip().strip('"').strip("'") for c in f.readline().split(",")]
    except OSError:
        return []


def _points(path: Path) -> np.ndarray | None:
    try:
        data = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    except (OSError, ValueError):
        return None
    return data if data.size else None


def to_grid(x: np.ndarray, y: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Scattered points on a regular grid -> (grid[y, x], xs, ys); nan where there is no point."""
    xs, ys = np.unique(x), np.unique(y)
    grid = np.full((ys.size, xs.size), np.nan)
    grid[np.searchsorted(ys, y), np.searchsorted(xs, x)] = values
    return grid, xs, ys


@dataclass
class Field:
    grid: np.ndarray          # [row, column] in the order of ys, xs
    xs: np.ndarray            # point centres, image pixels
    ys: np.ndarray
    valid: float = 1.0        # share of points that converged

    @property
    def ok(self) -> bool:
        return self.xs.size >= 2 and self.ys.size >= 2

    def finite(self) -> np.ndarray:
        return self.grid[np.isfinite(self.grid)]


def displacement_field(path: Path, component: str = "v") -> Field | None:
    data = _points(path)
    if data is None or data.shape[1] < 4:
        return None
    x, y, u, v = data[:, 0], data[:, 1], data[:, 2], data[:, 3]
    conv = data[:, 5].astype(bool) if data.shape[1] >= 6 else np.ones(len(x), dtype=bool)
    values = {"u": u, "v": v, "mag": np.hypot(u, v)}[component]
    grid, xs, ys = to_grid(x, y, np.where(conv, values, np.nan))
    return Field(grid, xs, ys, float(conv.mean()) if conv.size else 0.0)


def strain_field(path: Path, column: str = DEFAULT_STRAIN) -> Field | None:
    header = csv_header(path)
    if column not in header:
        return None
    data = _points(path)
    if data is None or data.shape[1] <= header.index(column):
        return None
    values = data[:, header.index(column)]
    grid, xs, ys = to_grid(data[:, 0], data[:, 1], values)
    return Field(grid, xs, ys, float(np.isfinite(values).mean()) if values.size else 0.0)


def strain_columns(path: Path) -> list[str]:
    """The strain components in a strain CSV, eps_yy first."""
    cols = [c for c in csv_header(path)[2:] if c.startswith("eps")]
    return sorted(cols, key=lambda c: (c != DEFAULT_STRAIN, c))


def sample(items: list, n: int = 40) -> list:
    if len(items) <= n:
        return list(items)
    return [items[int(round(k))] for k in np.linspace(0, len(items) - 1, n)]


def color_limits(fields: list[Field | None], symmetric: bool = False) -> tuple[float, float]:
    """2nd to 98th percentile of the values (symmetric about 0 if asked)."""
    values = [f.finite() for f in fields if f is not None and f.finite().size]
    if not values:
        return (-1.0, 1.0) if symmetric else (0.0, 1.0)
    allv = np.concatenate(values)
    lo, hi = float(np.percentile(allv, 2)), float(np.percentile(allv, 98))
    if symmetric:
        m = max(abs(lo), abs(hi), 1e-9)
        return -m, m
    if hi - lo < 1e-12:
        lo, hi = lo - 1e-6, hi + 1e-6
    return lo, hi


def mean_series(files: dict[int, Path], read) -> tuple[np.ndarray, np.ndarray]:
    """(frame indices, mean of each field) for a plot against time."""
    idx, means = [], []
    for frame, path in files.items():
        f = read(path)
        if f is not None and f.finite().size:
            idx.append(frame)
            means.append(float(np.mean(f.finite())))
    return np.array(idx, dtype=np.int64), np.array(means)


# --------------------------------------------------------------- crack tips --

def crack_length(x: float, notch: tuple[int, int] | None, from_left: bool = True) -> float:
    """Crack length (px) from the notch tip along the crack direction."""
    if notch is None or not math.isfinite(x):
        return math.nan
    return max(0.0, (x - notch[0]) if from_left else (notch[0] - x))


def tracker_lengths(run: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """tracker -> (frame indices, crack length px) from the trackers' CSVs."""
    out = {}
    for name, file in (("image tracker", "crack_tip_image.csv"), ("opening method", "crack_tip_opening.csv")):
        path = Path(run) / file
        if not path.is_file():
            continue
        header, rows = _read_table(path)
        if "frame_index" in header and "crack_length_px" in header:
            out[name] = (_column(header, rows, "frame_index", int, -1), _column(header, rows, "crack_length_px"))
    return out


def peak_crack_speed(run: Path) -> float | None:
    path = Path(run) / "crack_tip_opening.csv"
    if not path.is_file():
        return None
    header, rows = _read_table(path)
    if "crack_speed_px_s" not in header:
        return None
    speed = _column(header, rows, "crack_speed_px_s")
    return float(np.nanmax(speed)) if np.isfinite(speed).any() else None


def read_manual(run: Path) -> dict[int, tuple[float, float]]:
    path = Path(run) / MANUAL_FILE
    if not path.is_file():
        return {}
    header, rows = _read_table(path)
    if not {"frame_index", "crack_tip_x_px", "crack_tip_y_px"} <= set(header):
        return {}
    idx = _column(header, rows, "frame_index", int, -1)
    x, y = _column(header, rows, "crack_tip_x_px"), _column(header, rows, "crack_tip_y_px")
    return {int(i): (float(a), float(b)) for i, a, b in zip(idx, x, y)
            if i >= 0 and math.isfinite(a) and math.isfinite(b)}


def write_manual(run: Path, marks: dict[int, tuple[float, float]], times: dict[int, float]) -> None:
    """crack_tip_viewer.py's format (it and crack_tip_image.py read it too)."""
    lines = [MANUAL_HEADER]
    for n in sorted(marks):
        x, y = marks[n]
        t = times.get(n, math.nan)
        lines.append(f"{n},frame_{n:06d}.tif,{x:.1f},{y:.1f},{t:.6f}")
    path = Path(run) / MANUAL_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------- validation --

@dataclass
class Check:
    level: str                # "ok", "warn", "error", or "pending" (still checking)
    text: str
    fix: str | None = None    # an action the DIC tab offers: "use_current_roi"

    @property
    def mark(self) -> str:
        return {"ok": "✓", "warn": "⚠", "error": "✗", "pending": "…"}[self.level]


def synced(run: Path) -> bool:
    try:
        with open(Path(run) / "frames.csv", encoding="utf-8") as f:
            return "time_s" in f.readline().strip().split(",")
    except OSError:
        return False


def frames_to_analyse(res: RunResults, overview_every: int, crack_every: int, redo: bool) -> tuple[int, int, int]:
    """(whole-test frames, full-rate frames, of those already with results) the DIC job would select."""
    saved = np.asarray(res.frames.index, dtype=np.int64) if res.frames is not None else np.zeros(0, np.int64)
    overview, window = dic_selection(saved, res.windows, max(1, overview_every), max(1, crack_every))
    done = set(dic_files(res.run)) if res.run is not None else set()
    chosen = set(overview) | set(window)
    return len(overview), len(window), 0 if redo else len(chosen & done)


def validate(res: RunResults, options, *, current_roi: Roi | None = None, env: tuple[bool, str] | None = None,
             scripts: list[ScriptCheck] = (), disk=None, busy: bool = False) -> list[Check]:
    """What stops DIC (and what follows it) from running on this test, step by step.

    `options` is a postprocess.Options with the DIC tab's choices; `env` the camera
    environment check (None = still checking); `disk` a storage.DiskStatus."""
    checks: list[Check] = []
    add = checks.append
    steps = [options.run_dic, options.run_strain, options.crack_tip_image, options.crack_tip_opening]
    if not any(steps):
        add(Check("error", "Nothing to run: tick DIC, strain or a crack tracker."))
    if res.run is None or res.frames is None:
        add(Check("error", "This test has no camera recording."))
        return checks
    # 1. images
    if res.images_deleted:
        add(Check("error", "The camera images were deleted: DIC cannot run on this test any more."))
    elif not res.has_images:
        add(Check("error", "No saved camera frames."))
    else:
        ref = res.images_dir / "frame_000000.tif"
        if ref.is_file():
            add(Check("ok", f"{len(res.frames)} saved frames; reference frame 0 present."))
        else:
            add(Check("error", "Reference frame 0 (images/frame_000000.tif) is missing: DIC compares every frame "
                               "with it."))
    # 2. ROI
    h, w = int(res.settings.get("height") or 0), int(res.settings.get("width") or 0)
    roi = res.roi
    current_fits = current_roi is not None and not current_roi.problems() and (not h or current_roi.matches(h, w))
    if roi is None:
        problem = "No ROI saved with this recording"
    elif roi.problems():
        problem = "The ROI cannot be used: " + "; ".join(roi.problems())
    elif h and not roi.matches(h, w):
        problem = f"The ROI was drawn for {roi.shape[1]}×{roi.shape[0]} frames, these are {w}×{h}"
    else:
        problem = None
    if problem is None:
        add(Check("ok", "ROI and seed point OK."))
    elif current_fits:
        add(Check("warn", f"{problem}. The Camera tab's ROI fits these frames: use it, or draw one for this test.",
                  "use_current_roi"))
    elif options.run_dic:
        add(Check("error", f"{problem}. Draw one for this test: Draw ROI on a frame... below."))
    else:
        add(Check("warn", f"{problem}."))
    # 3. notch
    roi_for_notch = roi if problem is None else (current_roi if current_fits else None)
    trackers = options.crack_tip_image or options.crack_tip_opening
    if roi_for_notch is not None and roi_for_notch.notch:
        add(Check("ok", f"Notch tip at x {roi_for_notch.notch[0]}, y {roi_for_notch.notch[1]}."))
    elif trackers:
        add(Check("warn", "No notch tip: the crack trackers will be skipped (Draw ROI on a frame... > Place notch tip)."))
    # 4. sync
    if synced(res.run):
        add(Check("ok", "Frames are on data.csv's clock."))
    else:
        add(Check("warn", "Frames are not synced to data.csv yet: that runs first."))
    # 5. frames to analyse
    if options.run_dic and res.has_images:
        n_over, n_win, n_done = frames_to_analyse(res, options.overview_every, options.crack_every, options.redo)
        todo = n_over + n_win - n_done
        text = f"DIC on {n_over} whole-test + {n_win} full-rate frames"
        if n_done:
            text += f"; {n_done} already have results and are skipped (tick Redo to run them again)"
        if todo <= 0 and not any(steps[1:]):
            add(Check("error", text + ": nothing left to do."))
        else:
            add(Check("ok" if todo > 0 else "warn", text + "."))
    # 6. steps that need DIC results
    has_dic = bool(dic_files(res.run))
    if (options.run_strain or trackers) and not options.run_dic and not has_dic:
        add(Check("error", "Strain and the crack trackers need DIC results: tick DIC."))
    n_strain, n_dic = len(strain_files(res.run)), len(dic_files(res.run))
    if n_strain > n_dic and n_dic:
        add(Check("warn", f"{n_strain} strain files but {n_dic} DIC results: files from an earlier run are mixed in."))
    # 7. scripts
    needed = {CAPTURE: options.run_dic, DIC_RUN: options.run_strain, CRACK_IMAGE: options.crack_tip_image,
              CRACK_OPENING: options.crack_tip_opening}
    bad = [c for c in scripts if needed.get(c.name) and not c.ok]
    if bad:
        for c in bad:
            add(Check("error", f"Camera script: {c.text()}"))
    elif scripts:
        add(Check("ok", "Camera scripts OK."))
    # 8. camera environment
    if env is None:
        add(Check("pending", "Checking the camera environment for pyvale..."))
    elif env[0]:
        add(Check("ok", f"Camera environment: {env[1]}."))
    else:
        add(Check("error", f"Camera environment: {env[1]}"))
    # 9. disk
    if disk is not None:
        free = disk.free / 1e9
        if disk.level == "critical":
            add(Check("error", f"Only {free:.1f} GB free: free some space first (Outputs tab)."))
        elif disk.level == "low":
            add(Check("warn", f"{free:.1f} GB free: DIC results need some room."))
        else:
            add(Check("ok", f"{free:.0f} GB free."))
    # 10. already running
    if busy:
        add(Check("error", "DIC is already queued or running for this test (Cancel stops it)."))
    return checks


def can_run(checks: list[Check]) -> bool:
    return not any(c.level in ("error", "pending") for c in checks)


def check_camera_env(python: str, env: dict | None = None, timeout: float = 90.0) -> tuple[bool, str]:
    """Does the camera environment's Python import pyvale and OpenCV? (ok, version or the reason)."""
    code = "import pyvale, cv2; print(getattr(pyvale, '__version__', '?'), cv2.__version__)"
    try:
        done = subprocess.run([python, "-c", code], env=env, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"cannot run {python}: {e}"
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["unknown error"])[-1]
        return False, (f"{last} (in {python}). On the Pi: bash scripts/install_pi.sh --camera; on a PC: set "
                       "[camera] python in config.toml.")
    parts = done.stdout.split()
    return True, f"pyvale {parts[0] if parts else '?'}, OpenCV {parts[1] if len(parts) > 1 else '?'}"
