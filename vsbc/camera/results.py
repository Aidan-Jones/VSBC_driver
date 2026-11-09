"""A finished test (or camera recording) read back for the GUI's Results tab,
and the frames where the crack happened.

Where the crack is: the Mega's break detection compares load samples, so the
break happened between the last reading before the load fell (the break
load, data.csv row `brk`) and the next reading. The frames between those two
times (moved back by the load-cell delay, onto the frames' clock) are the
crack interval: about 48 frames at 10 load samples/s and 478 fps, 6 at 80/s.
The crack window adds CRACK_CONTEXT_FRAMES saved frames either side. Inside
the interval, the largest change between consecutive frames (the specimen
separating, or springing back) is the estimated crack frame.

NumPy and the standard library only: runs in the GUI's environment.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from vsbc.analysis import find_break
from vsbc.camera import tiff
from vsbc.camera.roi import Roi, load_roi
from vsbc.camera.sync import _column, _read_table, in_windows, nearest_index, read_json
from vsbc.storage import IMAGES_DELETED_FILE

CRACK_CONTEXT_FRAMES = 50       # saved frames shown before and after the crack interval
ESTIMATE_MARGIN_FRAMES = 10     # the estimate also looks this far outside the interval
ESTIMATE_DOWNSAMPLE = 4


@dataclass
class Frames:
    """The saved frames, in frame order."""

    index: np.ndarray           # camera frame number (frame_index)
    files: list[str]
    time_s: np.ndarray          # data.csv's clock (nan before the sync)
    load_n: np.ndarray
    extension_mm: np.ndarray
    full_rate: np.ndarray       # bool: inside a full-rate window
    markers: list[str]

    def __len__(self) -> int:
        return int(self.index.size)

    def position_of(self, frame_index: int) -> int:
        """Position in this list of the saved frame nearest a camera frame number."""
        return int(nearest_index(self.index, np.array([frame_index]))[0])

    def position_at(self, t: float) -> int:
        """Position of the saved frame nearest time t (s)."""
        ok = np.isfinite(self.time_s)
        if not ok.any():
            return 0
        pos = np.where(ok)[0]
        return int(pos[nearest_index(self.time_s[pos], np.array([t]))[0]])


@dataclass
class CrackWindow:
    source: str                 # "break" or "high-speed window"
    note: str
    t_hold: float | None = None     # last reading before the drop (s, data.csv clock)
    t_drop: float | None = None     # first reading after it
    break_load_n: float | None = None
    first: int = 0              # crack interval: positions in Frames (inclusive), or first > last if empty
    last: int = -1
    start: int = 0              # crack window (interval + context), positions in Frames
    end: int = -1
    estimate: int | None = None     # position of the estimated crack frame
    full_rate: bool = False         # every frame was saved around the interval

    @property
    def n_frames(self) -> int:
        return max(0, self.last - self.first + 1)

    def contains(self, position: int) -> bool:
        return self.first <= position <= self.last


@dataclass
class RunResults:
    folder: Path                # what was opened: the test folder, or the recording folder
    test: Path | None           # the test folder (data.csv), if any
    run: Path | None            # the camera folder (frames.csv), if any
    name: str = ""
    meta: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    summary_text: str = ""
    settings: dict = field(default_factory=dict)    # capture_settings.json
    sync: dict = field(default_factory=dict)        # sync.json
    data: dict = field(default_factory=dict)        # data.csv columns: time_s, load_N, extension_mm
    frames: Frames | None = None
    roi: Roi | None = None
    tips: dict = field(default_factory=dict)        # tracker name -> {frame_index: (x, y)}
    plots: list[Path] = field(default_factory=list)
    video: Path | None = None
    images_deleted: bool = False
    crack: CrackWindow | None = None

    @property
    def images_dir(self) -> Path | None:
        return self.run / "images" if self.run is not None else None

    @property
    def has_images(self) -> bool:
        return self.frames is not None and len(self.frames) > 0 and not self.images_deleted \
            and self.images_dir is not None and self.images_dir.is_dir()

    @property
    def load_delay_s(self) -> float:
        value = self.sync.get("load_delay_ms")
        return float(value) / 1000.0 if isinstance(value, (int, float)) else 0.0

    @property
    def windows(self) -> list[list[int]]:
        return [list(w) for w in self.settings.get("full_rate_windows") or [] if len(w) == 2]

    def image_path(self, position: int) -> Path:
        return self.images_dir / self.frames.files[position]

    def dic_path(self, position: int) -> Path:
        return self.run / "pyvale_results" / f"dic_results_frame_{int(self.frames.index[position]):06d}.csv"

    def tips_at(self, position: int) -> dict[str, tuple[float, float]]:
        idx = int(self.frames.index[position])
        return {name: tips[idx] for name, tips in self.tips.items() if idx in tips}


# ------------------------------------------------------------------- reading --

def resolve(folder: Path) -> tuple[Path | None, Path | None]:
    """(test folder, camera folder) for a test folder, its camera/ folder, or a camera recording."""
    folder = Path(folder)
    if (folder / "data.csv").is_file():
        run = folder / "camera"
        return folder, run if (run / "frames.csv").is_file() else None
    if (folder / "frames.csv").is_file():
        test = folder.parent if folder.name == "camera" and (folder.parent / "data.csv").is_file() else None
        return test, folder
    raise ValueError(f"{folder} is neither a test (data.csv) nor a camera recording (frames.csv)")


def _read_frames(run: Path) -> Frames:
    header, rows = _read_table(run / "frames.csv")
    saved = _column(header, rows, "saved", int, 0).astype(bool)
    index = _column(header, rows, "frame_index", int, -1)
    files_col = header.index("filename") if "filename" in header else None
    marker_col = header.index("marker") if "marker" in header else None
    keep = np.where(saved & (index >= 0))[0]
    order = keep[np.argsort(index[keep], kind="stable")]

    def cells(col):
        return [rows[k][col] if col is not None and col < len(rows[k]) else "" for k in order]

    files = [f or f"frame_{int(index[k]):06d}.tif" for f, k in zip(cells(files_col), order)]
    time_s = _column(header, rows, "time_s")
    if not np.isfinite(time_s).any():                       # not synced yet: the camera process's own clock
        time_s = _column(header, rows, "pc_time_s")
    windows = read_json(run / "capture_settings.json").get("full_rate_windows") or []
    full_rate = _column(header, rows, "full_rate", int, 0).astype(bool) | in_windows(index, windows)
    return Frames(index[order], files, time_s[order], _column(header, rows, "load_N")[order],
                  _column(header, rows, "extension_mm")[order], full_rate[order], cells(marker_col))


def _read_tips(path: Path, x_name: str, y_name: str | None, y_default: float | None) -> dict:
    if not path.is_file():
        return {}
    header, rows = _read_table(path)
    if "frame_index" not in header or x_name not in header:
        return {}
    idx = _column(header, rows, "frame_index", int, -1)
    x = _column(header, rows, x_name)
    y = _column(header, rows, y_name) if y_name and y_name in header else np.full(len(rows), np.nan)
    if y_default is not None:
        y = np.where(np.isfinite(y), y, y_default)
    return {int(i): (float(a), float(b)) for i, a, b in zip(idx, x, y) if i >= 0 and math.isfinite(a)
            and math.isfinite(b)}


def _plots(test: Path | None, run: Path | None) -> list[Path]:
    out = []
    if test is not None and (test / "plot.png").is_file():
        out.append(test / "plot.png")
    for folder in ((run / "plots") if run else None, (run / "pyvale_results") if run else None):
        if folder is not None and folder.is_dir():
            out += sorted(p for p in folder.glob("*.png") if p.is_file())
    return out


def _read_data(test: Path) -> dict[str, np.ndarray]:
    path = test / "data.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = [row for row in reader if row]
    return {name: _column(header, rows, name) for name in ("time_s", "load_N", "extension_mm", "position_mm")}


def load_results(folder: Path) -> RunResults:
    """Everything the Results tab shows for one test or recording (no images are read)."""
    folder = Path(folder)
    test, run = resolve(folder)
    res = RunResults(folder, test, run)
    if test is not None:
        res.meta = read_json(test / "meta.json")
        res.summary = read_json(test / "summary.json")
        try:
            res.summary_text = (test / "summary.txt").read_text(encoding="utf-8")
        except OSError:
            res.summary_text = ""
        res.data = _read_data(test)
        res.name = (res.meta.get("specimen") or {}).get("specimen_id") or test.name
        if (test / "video.mp4").is_file():
            res.video = test / "video.mp4"
    if run is not None:
        res.settings = read_json(run / "capture_settings.json")
        res.sync = read_json(run / "sync.json")
        res.frames = _read_frames(run)
        res.roi = load_roi(run / "roi.npz")
        res.images_deleted = (run / IMAGES_DELETED_FILE).is_file() or not (run / "images").is_dir()
        notch_y = float(res.roi.notch[1]) if res.roi is not None and res.roi.notch else None
        res.tips = {k: v for k, v in (
            ("image tracker", _read_tips(run / "crack_tip_image.csv", "crack_tip_x_px", "crack_tip_y_px", None)),
            ("opening method", _read_tips(run / "crack_tip_opening.csv", "tip_x_raw_px", None, notch_y)),
            ("by hand", _read_tips(run / "crack_tip_manual.csv", "crack_tip_x_px", "crack_tip_y_px", None)),
        ) if v}
        if test is None:
            res.name = res.settings.get("test_name") or run.name
            if (run / "video.mp4").is_file():
                res.video = run / "video.mp4"
    res.plots = _plots(test, run)
    res.crack = find_crack(res)
    return res


# --------------------------------------------------------------------- crack --

def break_rows(res: RunResults) -> tuple[int, int] | None:
    """(data.csv row of the break load, the next row), as analyze() finds them; None without a break."""
    t, f, e = (res.data.get(k) for k in ("time_s", "load_N", "extension_mm"))
    if t is None or f is None or e is None or f.size < 3:
        return None
    spec = res.meta.get("specimen") or {}
    drop = float(spec.get("break_drop_pct", 40.0) or 0.0)
    floor = float(spec.get("break_min_n", 5.0) or 0.0)
    rows = np.where(np.isfinite(f) & np.isfinite(e))[0]     # analyze() works on these rows only
    brk = find_break(f[rows], drop, floor)
    if brk is None or brk + 1 >= rows.size:
        return None
    return int(rows[brk]), int(rows[brk + 1])


def _window(frames: Frames, first: int, last: int, context: int) -> tuple[int, int]:
    """The interval plus `context` saved frames either side (an empty interval: the frames either side)."""
    hi = max(last, first - 1)
    return max(0, min(first, len(frames) - 1) - context), min(len(frames) - 1, hi + context)


def find_crack(res: RunResults, context: int = CRACK_CONTEXT_FRAMES) -> CrackWindow | None:
    """The crack interval and window from the break load, else the last full-rate window."""
    frames = res.frames
    if frames is None or len(frames) == 0:
        return None
    rows = break_rows(res)
    if rows is not None and np.isfinite(frames.time_s).any():
        brk, after = rows
        t_hold, t_drop = float(res.data["time_s"][brk]), float(res.data["time_s"][after])
        load = float(res.data["load_N"][brk])
        delay = res.load_delay_s
        a, b = t_hold - delay, t_drop - delay
        t = np.where(np.isfinite(frames.time_s), frames.time_s, -np.inf)
        inside = np.where((t > a) & (t <= b))[0]
        crack = CrackWindow("break", "", t_hold, t_drop, load)
        if inside.size:
            crack.first, crack.last = int(inside[0]), int(inside[-1])
        else:                                   # no frame in between: the saved frames either side
            later = np.where(t > b)[0]
            crack.first = int(later[0]) if later.size else len(frames)
            crack.last = crack.first - 1
        crack.start, crack.end = _window(frames, crack.first, crack.last, context)
        span = f"between t = {t_hold:.3f} s and {t_drop:.3f} s"
        near = frames.full_rate[max(crack.first - 1, 0):min(crack.last + 2, len(frames))]
        crack.full_rate = bool(near.size and near.all())
        if crack.full_rate:
            crack.note = f"The load dropped {span}: {crack.n_frames} frame(s) in between, at full rate."
        else:
            before = frames.index[crack.first - 1] if crack.first > 0 else None
            nxt = frames.index[crack.first] if crack.first < len(frames) else None
            between = (f"between frame {before} and frame {nxt}" if before is not None and nxt is not None
                       else "at the edge of the recording")
            crack.note = (f"The load dropped {span}, {between}: only {crack.n_frames} frame(s) saved in between. "
                          "Turn on \"Save full-rate frames at the break\" (Camera panel) for full-rate frames.")
        return crack
    windows = res.windows
    if windows:
        start, end = windows[-1]
        sel = np.where((frames.index >= start) & (frames.index <= end))[0]
        if sel.size:
            labels = res.settings.get("window_labels") or []
            label = labels[len(windows) - 1] if len(labels) >= len(windows) else "high speed"
            crack = CrackWindow("high-speed window", f"No break detected: showing the last full-rate window "
                                                     f"({label}, {sel.size} frames).")
            crack.first, crack.last = int(sel[0]), int(sel[-1])
            crack.start, crack.end = crack.first, crack.last
            return crack
    return None


def frame_changes(images: list[np.ndarray], box: tuple[int, int, int, int] | None = None,
                  step: int = ESTIMATE_DOWNSAMPLE) -> np.ndarray:
    """Mean |difference| between each image and the one before (first entry nan)."""
    out = np.full(len(images), np.nan)
    prev = None
    for k, image in enumerate(images):
        if image is None:
            prev = None
            continue
        a = image if box is None else image[box[1]:box[3], box[0]:box[2]]
        a = a[::step, ::step].astype(np.int16)
        if prev is not None and prev.shape == a.shape:
            out[k] = float(np.mean(np.abs(a - prev)))
        prev = a
    return out


def estimate_crack_frame(res: RunResults, crack: CrackWindow | None = None,
                         read: Callable[[Path], np.ndarray] = tiff.read) -> int | None:
    """Position of the frame with the largest change from the one before, in and around the crack interval."""
    crack = crack or res.crack
    if crack is None or crack.source != "break" or not crack.full_rate or crack.n_frames < 1             or not res.has_images:
        return None
    # The load-cell delay is only roughly known: look a little outside the interval too.
    margin = min(ESTIMATE_MARGIN_FRAMES, max(2, crack.n_frames))
    lo = max(crack.start, crack.first - margin)
    hi = min(crack.end, crack.last + margin)
    images = []
    for pos in range(lo, hi + 1):
        try:
            images.append(read(res.image_path(pos)))
        except (OSError, ValueError):
            images.append(None)
    box = None
    if res.roi is not None and res.roi.mask.any():
        ys, xs = np.nonzero(res.roi.mask)
        box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    changes = frame_changes(images, box)
    # Only consecutive camera frames compare fairly (a gap means more time between them).
    idx = res.frames.index[lo:hi + 1]
    gaps = np.r_[np.inf, np.diff(idx)]
    changes[gaps > 1] = np.nan
    if not np.isfinite(changes).any():
        return None
    crack.estimate = lo + int(np.nanargmax(changes))
    return crack.estimate
