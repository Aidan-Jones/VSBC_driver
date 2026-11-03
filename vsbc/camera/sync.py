"""One clock for data.csv, the camera frames and the video.

Three clocks are involved: the Mega's millis() (data.csv time_s), the camera's
own timestamp counter, and the Pi's monotonic clock (time.perf_counter_ns(),
the same in every process). During a recording the GUI logs the Pi time at
which every load sample arrived (camera/samples.csv), and the camera process
logs the Pi time at which every frame arrived plus, every 2 s, a latch of the
camera clock bracketed by two Pi times (camera/camera_clock.csv).

Linear fits map the camera clock onto the Pi's, and the Pi's onto the Mega's,
so every frame gets a time_s on data.csv's clock with data.csv's zero (the
first RUN sample). Transport delays are always positive, so the Mega fit
follows the lower envelope of the arrival times (the samples that came
through fastest); the slope is fitted too, since a Mega clone's ceramic
resonator can be 0.5 % off.

Then every saved frame gets a load: interpolated to the frame's time, or, in
a full-rate window (the break), the latest reading at or before the frame,
so nothing is interpolated across the break. Every data.csv row gets the
saved frame nearest its time.

Pure functions on NumPy arrays plus the file I/O; runs in both environments.
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from vsbc.units import Units, units_from_dict

FRAME_SYNC_COLUMNS = ("time_s", "load_N", "load_method", "sample_row", "position_mm", "extension_mm")
DATA_SYNC_COLUMNS = ("frame", "frame_dt_ms")
CRACK_SYNC_COLUMNS = ("test_time_s", "load_N", "extension_mm")
MAX_SLOPE_ERROR = 0.02     # clocks within 2 % of each other (a resonator is ~0.5 %)


# ------------------------------------------------------------------ clock fits --

@dataclass
class ClockFit:
    """y = y0 + slope * (x - x0), with x and y in ns."""

    slope: float
    x0: int
    y0: float
    n: int = 0
    delay_p50_ms: float = math.nan
    delay_p95_ms: float = math.nan
    method: str = ""

    def __call__(self, x) -> np.ndarray:
        x = np.asarray(x)
        d = x - self.x0 if x.dtype.kind in "iu" else x.astype(float) - self.x0
        return self.y0 + self.slope * np.asarray(d, dtype=float)

    def inverse(self, y) -> np.ndarray:
        return self.x0 + (np.asarray(y, dtype=float) - self.y0) / self.slope

    @property
    def ppm(self) -> float:
        return (self.slope - 1.0) * 1e6

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ppm"] = self.ppm
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in d.items()}


def _polyfit_slope(dx: np.ndarray, dy: np.ndarray) -> float:
    if dx.size < 2 or np.ptp(dx) <= 0:
        return 1.0
    slope = float(np.polyfit(dx, dy, 1)[0])
    return slope if abs(slope - 1.0) <= MAX_SLOPE_ERROR else 1.0


def fit_lower_envelope(sent_ns, arrived_ns, keep: float = 0.2, rounds: int = 4) -> ClockFit:
    """Line along the fastest arrivals: arrived ≈ y0 + slope * (sent - x0) + delay, delay >= 0."""
    x = np.asarray(sent_ns, dtype=np.int64)
    y = np.asarray(arrived_ns, dtype=np.int64)
    if x.size == 0:
        raise ValueError("no clock pairs to fit")
    x0, y0 = int(x[0]), int(y[0])
    dx = (x - x0).astype(float)
    dy = (y - y0).astype(float)
    slope = _polyfit_slope(dx, dy)
    for _ in range(rounds):
        r = dy - slope * dx
        fast = r <= np.quantile(r, keep)
        if fast.sum() < 2:
            break
        slope = _polyfit_slope(dx[fast], dy[fast])
    r = dy - slope * dx
    floor = float(np.quantile(r, 0.01)) if r.size > 20 else float(r.min())
    delays_ms = (r - floor) / 1e6
    return ClockFit(slope, x0, y0 + floor, int(x.size), float(np.median(delays_ms)),
                    float(np.quantile(delays_ms, 0.95)), "lower envelope")


def fit_latches(latches: np.ndarray) -> ClockFit | None:
    """Camera -> Pi from (host_before, camera, host_after) rows: the latch happened between the two."""
    if latches is None or len(latches) == 0:
        return None
    before, cam, after = (np.asarray(latches[:, i], dtype=np.int64) for i in range(3))
    bracket = after - before
    good = bracket <= 2 * np.median(bracket) + 1000
    before, cam, bracket = before[good], cam[good], bracket[good]
    mid = before + bracket // 2
    x0, y0 = int(cam[0]), int(mid[0])
    dx = (cam - x0).astype(float)
    dy = (mid - y0).astype(float)
    slope = _polyfit_slope(dx, dy)
    intercept = float(np.mean(dy - slope * dx))
    half_ms = bracket / 2e6
    return ClockFit(slope, x0, y0 + intercept, int(cam.size), float(np.median(half_ms)),
                    float(np.quantile(half_ms, 0.95)), "timestamp latch")


def fit_camera_clock(latches: np.ndarray | None, frame_cam_ns, frame_host_ns,
                     arrival_latency_ns: float = 0.0) -> ClockFit:
    """Camera -> Pi. Without latches: the earliest frame arrivals, minus the exposure + transfer time."""
    fit = fit_latches(latches)
    if fit is not None:
        return fit
    fit = fit_lower_envelope(frame_cam_ns, frame_host_ns)
    fit.y0 -= arrival_latency_ns
    fit.method = "frame arrival times"
    return fit


# ------------------------------------------------------------ frame <-> load --

def nearest_index(sorted_t: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Index of the value in sorted_t nearest each t (sorted_t ascending, non-empty)."""
    sorted_t = np.asarray(sorted_t, dtype=float)
    t = np.asarray(t, dtype=float)
    right = np.clip(np.searchsorted(sorted_t, t), 0, sorted_t.size - 1)
    left = np.clip(right - 1, 0, sorted_t.size - 1)
    use_left = np.abs(t - sorted_t[left]) <= np.abs(sorted_t[right] - t)
    return np.where(use_left, left, right)


def latest_index(sorted_t: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Index of the last sorted_t value at or before each t; -1 before the first."""
    return np.searchsorted(np.asarray(sorted_t, dtype=float), np.asarray(t, dtype=float), side="right") - 1


def interpolate(t: np.ndarray, sample_t: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Linear interpolation; nan outside the samples' time span."""
    t = np.asarray(t, dtype=float)
    out = np.interp(t, sample_t, values)
    out[(t < sample_t[0]) | (t > sample_t[-1])] = np.nan
    return out


def in_windows(frame_index: np.ndarray, windows) -> np.ndarray:
    inside = np.zeros(len(frame_index), dtype=bool)
    for start, end in windows or []:
        inside |= (frame_index >= start) & (frame_index <= end)
    return inside


def video_schedule(saved_t: np.ndarray, fps: float, t_end: float, t_start: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """(video frame times, index into saved_t of the frame shown at each): constant fps from t_start."""
    times = t_start + np.arange(int(math.floor((t_end - t_start) * fps + 1e-9)) + 1) / fps
    if saved_t.size == 0:
        return times, np.zeros(0, dtype=int)
    return times, nearest_index(saved_t, times)


# ------------------------------------------------------------------ file I/O --

def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        return header, [row for row in reader if row]


def _write_table(path: Path, header: list[str], rows) -> None:
    """Atomic: a crash while writing leaves the old file in place."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    os.replace(tmp, path)


def _column(header: list[str], rows: list[list[str]], name: str, dtype=float, default=math.nan) -> np.ndarray:
    """One column as an array; all `default` when the file has no such column."""
    if name not in header:
        return np.full(len(rows), default, dtype=np.int64 if dtype is int else float)
    i = header.index(name)
    out = []
    for row in rows:
        cell = row[i] if i < len(row) else ""
        try:
            out.append(dtype(cell) if cell != "" else default)
        except ValueError:
            out.append(default)
    return np.array(out, dtype=np.int64 if dtype is int else float)


def _strip_columns(header: list[str], rows: list[list[str]], names) -> tuple[list[str], list[list[str]]]:
    """Remove columns an earlier sync added, so syncing again does not duplicate them."""
    keep = [i for i, h in enumerate(header) if h not in names]
    return [header[i] for i in keep], [[row[i] if i < len(row) else "" for i in keep] for row in rows]


def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    def default(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return None if not math.isfinite(obj) else float(obj)
        if isinstance(obj, Path):
            return str(obj)
        return str(obj)

    def clean(obj):
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean(v) for v in obj]
        return obj

    tmp = Path(path).with_name(Path(path).name + ".tmp")
    tmp.write_text(json.dumps(clean(data), indent=2, default=default), encoding="utf-8")
    os.replace(tmp, path)


def _cell(value: float, digits: int) -> str:
    return "" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def _unit_frame_columns(units: Units) -> list[str]:
    cols = []
    if units.force != "N":
        cols.append(f"load_{units.force}")
    if units.length != "mm":
        cols += [f"position_{units.length}", f"extension_{units.length}"]
    return cols


# --------------------------------------------------------------------- sync --

@dataclass
class FrameTable:
    header: list[str]
    rows: list[list[str]]
    index: np.ndarray
    cam_ns: np.ndarray
    host_ns: np.ndarray
    saved: np.ndarray


def read_frames(cam_dir: Path) -> FrameTable:
    header, rows = _read_table(Path(cam_dir) / "frames.csv")
    header, rows = _strip_columns(header, rows, FRAME_SYNC_COLUMNS + tuple(
        h for h in header if h.startswith(("load_", "position_", "extension_")) and h not in FRAME_SYNC_COLUMNS))
    return FrameTable(header, rows, _column(header, rows, "frame_index", int, -1),
                      _column(header, rows, "camera_timestamp_ns", int, -1),
                      _column(header, rows, "host_ns", int, -1),
                      _column(header, rows, "saved", int, 0).astype(bool))


def _read_samples(cam_dir: Path) -> dict[str, np.ndarray] | None:
    path = Path(cam_dir) / "samples.csv"
    if not path.is_file():
        return None
    header, rows = _read_table(path)
    if not rows:
        return None
    t_ms = _column(header, rows, "t_ms", float, -1.0)     # three decimals from protocol v3, whole ms before
    # A Mega reset restarts millis(): keep the part before it (the part data.csv has).
    resets = np.where(np.diff(t_ms) < 0)[0]
    end = int(resets[0]) + 1 if resets.size else len(t_ms)
    return {
        "t_ms": t_ms[:end],
        "host_ns": _column(header, rows, "host_ns", int, -1)[:end],
        "position_mm": _column(header, rows, "position_mm")[:end],
        "load_n": _column(header, rows, "load_N")[:end],
    }


def _read_latches(cam_dir: Path) -> np.ndarray | None:
    path = Path(cam_dir) / "camera_clock.csv"
    if not path.is_file():
        return None
    header, rows = _read_table(path)
    if not rows:
        return None
    return np.column_stack([_column(header, rows, name, int, -1)
                            for name in ("host_before_ns", "camera_ns", "host_after_ns")])


def _arrival_latency_ns(settings: dict) -> float:
    """Exposure plus USB transfer of one frame (about 350 MB/s), for the fit without latches."""
    exposure = float(settings.get("exposure_us") or 0.0) * 1e3
    nbytes = float(settings.get("width") or 0) * float(settings.get("height") or 0)
    return exposure + nbytes / 350e6 * 1e9


def sync_camera(cam_dir: Path, data_csv: Path | None = None, meta: dict | None = None,
                load_delay_ms: float = 0.0) -> dict:
    """Give every frame a time_s (and every saved frame a load) and every data.csv row a frame.

    With a test (data_csv and its meta.json): data.csv's clock and zero. Without
    one (a recording made from the Camera tab): zero at frame 0. Rewrites
    camera/frames.csv and data.csv (atomically) and writes camera/sync.json.
    Returns the summary that also goes into sync.json.
    """
    cam_dir = Path(cam_dir)
    meta = meta or {}
    settings = read_json(cam_dir / "capture_settings.json")
    units = units_from_dict(meta.get("units") or settings.get("units"))
    frames = read_frames(cam_dir)
    if frames.index.size == 0:
        raise ValueError(f"{cam_dir / 'frames.csv'} lists no frames")
    samples = _read_samples(cam_dir)
    delay_s = load_delay_ms / 1000.0

    cam_fit = fit_camera_clock(_read_latches(cam_dir), frames.cam_ns, frames.host_ns,
                               _arrival_latency_ns(settings))
    host = cam_fit(frames.cam_ns)
    mega_fit = None
    if samples is not None and samples["t_ms"].size >= 2:
        mega_fit = fit_lower_envelope(np.rint(samples["t_ms"] * 1e6).astype(np.int64), samples["host_ns"])
        frame_mega_ms = mega_fit.inverse(host) / 1e6
        t0_ms = float(meta["mega_t0_ms"]) if meta.get("mega_t0_ms") is not None else float(frame_mega_ms[0])
        frame_t = (frame_mega_ms - t0_ms) / 1000.0
        sample_t = (samples["t_ms"] - t0_ms) / 1000.0 - delay_s
    else:
        frame_t = (host - host[0]) / 1e9
        sample_t = None
        t0_ms = None

    windows = settings.get("full_rate_windows") or []
    held = in_windows(frames.index, windows)
    saved = frames.saved
    load = np.full(frames.index.size, np.nan)
    pos = np.full(frames.index.size, np.nan)
    method = np.full(frames.index.size, "", dtype=object)
    if sample_t is not None and sample_t.size:
        interp_load = interpolate(frame_t, sample_t, samples["load_n"])
        last = latest_index(sample_t, frame_t)
        latest_load = np.where(last >= 0, samples["load_n"][np.clip(last, 0, None)], np.nan)
        latest_load[frame_t > sample_t[-1] + 0.5] = np.nan
        load = np.where(held, latest_load, interp_load)
        pos = interpolate(frame_t, sample_t + delay_s, samples["position_mm"])
        method = np.where(held, "latest", "interp").astype(object)
    start_pos = meta.get("start_position_mm")
    ext = pos - float(start_pos) if start_pos is not None else np.full_like(pos, np.nan)

    # data.csv: every row gets the saved frame nearest its time.
    data_rows = None
    pair_stats = {}
    sample_row = np.full(frames.index.size, -1)
    if data_csv is not None and Path(data_csv).is_file() and saved.any():
        d_header, d_rows = _read_table(Path(data_csv))
        d_header, d_rows = _strip_columns(d_header, d_rows, DATA_SYNC_COLUMNS)
        if d_rows:
            data_t = _column(d_header, d_rows, "time_s")
            target = data_t - delay_s
            saved_pos = np.where(saved)[0]
            saved_t = frame_t[saved_pos]
            order = np.argsort(saved_t)
            nearest = saved_pos[order][nearest_index(saved_t[order], target)]
            dt_ms = (frame_t[nearest] - target) * 1000.0
            data_rows = (d_header + list(DATA_SYNC_COLUMNS),
                         [row + [str(int(frames.index[k])), f"{dt:.3f}"] for row, k, dt in zip(d_rows, nearest, dt_ms)])
            within = np.abs(dt_ms)
            pair_stats = {"rows": len(d_rows), "frame_dt_ms_p50": float(np.median(within)),
                          "frame_dt_ms_p95": float(np.quantile(within, 0.95)),
                          "frame_dt_ms_max": float(within.max())}
            # Each saved frame's data.csv row: the one whose load it carries.
            frame_target = frame_t[saved_pos]
            rows_for = np.where(held[saved_pos], latest_index(target, frame_target), nearest_index(target, frame_target))
            inside = (frame_target >= target[0] - 0.5) & (frame_target <= target[-1] + 0.5)
            sample_row[saved_pos] = np.where(inside & (rows_for >= 0), rows_for, -1)

    # frames.csv
    unit_cols = _unit_frame_columns(units)
    header = frames.header + list(FRAME_SYNC_COLUMNS) + unit_cols
    out_rows = []
    for k, row in enumerate(frames.rows):
        if saved[k]:
            cells = [f"{frame_t[k]:.6f}", _cell(load[k], 3), method[k] if math.isfinite(load[k]) else "",
                     str(sample_row[k]) if sample_row[k] >= 0 else "", _cell(pos[k], 4), _cell(ext[k], 4)]
            if units.force != "N":
                cells.append(_cell(units.force_from_n(load[k]), units.force_digits + 1))
            if units.length != "mm":
                cells += [_cell(units.length_from_mm(pos[k]), units.length_digits),
                          _cell(units.length_from_mm(ext[k]), units.length_digits)]
        else:
            cells = [f"{frame_t[k]:.6f}"] + [""] * (len(FRAME_SYNC_COLUMNS) - 1 + len(unit_cols))
        out_rows.append(row + cells)
    _write_table(cam_dir / "frames.csv", header, out_rows)
    if data_rows is not None:
        _write_table(Path(data_csv), *data_rows)

    window_times = []
    for start, end in windows:
        sel = (frames.index >= start) & (frames.index <= end)
        if sel.any():
            window_times.append([float(frame_t[sel].min()), float(frame_t[sel].max())])
    summary = {
        "clock": "data.csv time_s (Mega millis, zero at the first RUN sample)" if meta.get("mega_t0_ms") is not None
                 else ("Mega millis, zero at frame 0" if mega_fit else "camera clock, zero at frame 0"),
        "mega_t0_ms": t0_ms,
        "load_delay_ms": load_delay_ms,
        "frames_grabbed": int(frames.index.size),
        "frames_saved": int(saved.sum()),
        "first_frame_time_s": float(frame_t[0]),
        "camera_to_pi": cam_fit.to_dict(),
        "mega_to_pi": mega_fit.to_dict() if mega_fit else None,
        "full_rate_windows_s": window_times,
        "data_csv_pairing": pair_stats,
        "units": units.to_dict(),
    }
    write_json(cam_dir / "sync.json", summary)
    return summary


def sync_test(test_folder: Path, load_delay_ms: float = 0.0) -> dict:
    """sync_camera for a test folder, plus a "camera" summary in its meta.json."""
    test_folder = Path(test_folder)
    meta = read_json(test_folder / "meta.json")
    summary = sync_camera(test_folder / "camera", test_folder / "data.csv", meta, load_delay_ms)
    meta["camera"] = {
        "folder": "camera",
        "frames_saved": summary["frames_saved"],
        "frames_grabbed": summary["frames_grabbed"],
        "full_rate_windows_s": summary["full_rate_windows_s"],
        "pairing": summary["data_csv_pairing"],
        "load_delay_ms": load_delay_ms,
        "sync": "camera/sync.json",
    }
    write_json(test_folder / "meta.json", meta)
    return summary


def annotate_crack_csv(path: Path, cam_dir: Path) -> bool:
    """Add test_time_s, load_N and extension_mm (from frames.csv, by frame_index) to a crack-tip CSV."""
    path, cam_dir = Path(path), Path(cam_dir)
    if not path.is_file() or not (cam_dir / "frames.csv").is_file():
        return False
    f_header, f_rows = _read_table(cam_dir / "frames.csv")
    if "time_s" not in f_header:
        return False
    lookup = {}
    ii, ti, li, ei = (f_header.index(c) for c in ("frame_index", "time_s", "load_N", "extension_mm"))
    for row in f_rows:
        lookup[row[ii]] = (row[ti], row[li], row[ei])
    header, rows = _read_table(path)
    header, rows = _strip_columns(header, rows, CRACK_SYNC_COLUMNS)
    fi = header.index("frame_index") if "frame_index" in header else 0
    out = [row + list(lookup.get(row[fi], ("", "", ""))) for row in rows]
    _write_table(path, header + list(CRACK_SYNC_COLUMNS), out)
    return True
