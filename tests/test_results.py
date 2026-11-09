"""The Results tab's reading of a test folder and its crack frames (no Qt)."""

import json

import numpy as np
import pytest

from vsbc.camera import tiff
from vsbc.camera.results import CRACK_CONTEXT_FRAMES, estimate_crack_frame, find_crack, load_results
from vsbc.camera.roi import Roi, rect_mask, save_roi

FPS = 100.0           # camera
RATE = 10.0           # load samples/s
BREAK_ROW = 30        # last reading before the load falls
SHAPE = (40, 64)


def _write_csv(path, header, rows):
    path.write_text(",".join(header) + "\n" + "".join(",".join(str(c) for c in r) + "\n" for r in rows),
                    encoding="utf-8")


def make_test(tmp_path, *, full_rate=True, break_detected=True, delay_ms=0.0, jump_frame=None, images=True,
              name="T1"):
    """A test folder: data.csv at 10/s that breaks after row 30, frames at 100 fps on data.csv's clock.

    With full_rate, every frame from 2.5 s to 3.5 s is saved (the break's RAM buffer); otherwise only the
    frame nearest each load sample. jump_frame: the frame index where the image changes suddenly."""
    test = tmp_path / name
    cam = test / "camera"
    (cam / "images").mkdir(parents=True)
    n = 40
    t = np.arange(n) / RATE
    load = np.minimum(t * 50.0, 1000.0)
    if break_detected:
        load[BREAK_ROW + 1:] = 2.0
    _write_csv(test / "data.csv", ["time_s", "position_mm", "extension_mm", "load_N", "state"],
               [[f"{a:.4f}", f"{a:.4f}", f"{a:.4f}", f"{b:.3f}", "R"] for a, b in zip(t, load)])
    (test / "meta.json").write_text(json.dumps({"specimen": {"specimen_id": name, "break_drop_pct": 40.0,
                                                             "break_min_n": 5.0}}), encoding="utf-8")
    (test / "summary.json").write_text("{}", encoding="utf-8")
    (test / "summary.txt").write_text("Peak load: 1500 N\n", encoding="utf-8")
    (test / "plot.png").write_bytes(b"png")

    frame_t = np.arange(int(4.0 * FPS)) / FPS
    window = (int(2.5 * FPS), int(3.5 * FPS)) if full_rate else None
    saved = set(np.rint(t * FPS).astype(int).tolist())
    if window:
        saved |= set(range(window[0], window[1] + 1))
    rows = []
    rng = np.random.default_rng(1)
    base = rng.integers(0, 255, SHAPE, dtype=np.uint8)
    for i, ft in enumerate(frame_t):
        is_saved = i in saved
        full = bool(window and window[0] <= i <= window[1])
        rows.append([i, f"frame_{i:06d}.tif", int(is_saved), i, i * 10_000_000, f"{ft:.6f}", "", i, 0, int(full),
                     f"{ft:.6f}", "", "", "", "", ""])
        if is_saved and images:
            img = base.copy()
            if jump_frame is not None and i >= jump_frame:
                img = 255 - img
            tiff.write_gray8(cam / "images" / f"frame_{i:06d}.tif", img)
    _write_csv(cam / "frames.csv", ["frame_index", "filename", "saved", "block_id", "camera_timestamp_ns",
                                    "pc_time_s", "marker", "host_ns", "matched", "full_rate", "time_s", "load_N",
                                    "load_method", "sample_row", "position_mm", "extension_mm"], rows)
    (cam / "capture_settings.json").write_text(json.dumps(
        {"full_rate_windows": [list(window)] if window else [], "window_labels": ["break"] if window else []}),
        encoding="utf-8")
    (cam / "sync.json").write_text(json.dumps({"load_delay_ms": delay_ms}), encoding="utf-8")
    save_roi(cam / "roi.npz", Roi(rect_mask(SHAPE, 4, 4, 60, 36), seed=(30, 20), notch=(5, 20)))
    (cam / "plots").mkdir()
    (cam / "plots" / "pyvale_dic_run_01.png").write_bytes(b"png")
    return test


def test_crack_interval_is_between_the_break_reading_and_the_next(tmp_path):
    res = load_results(make_test(tmp_path))
    crack = res.crack
    assert crack.source == "break"
    assert crack.t_hold == pytest.approx(3.0) and crack.t_drop == pytest.approx(3.1)
    assert crack.break_load_n == pytest.approx(150.0)
    times = res.frames.time_s
    assert times[crack.first] == pytest.approx(3.01) and times[crack.last] == pytest.approx(3.10)
    assert crack.n_frames == 10
    assert crack.first - crack.start == CRACK_CONTEXT_FRAMES
    assert crack.end == min(crack.last + CRACK_CONTEXT_FRAMES, len(res.frames) - 1)
    assert crack.full_rate and "full rate" in crack.note and "RAM buffer" not in crack.note
    assert res.plots and res.plots[0].name == "plot.png" and res.has_images


def test_load_cell_delay_moves_the_interval_earlier(tmp_path):
    res = load_results(make_test(tmp_path, delay_ms=50.0))
    assert res.frames.time_s[res.crack.first] == pytest.approx(2.96)
    assert res.crack.n_frames == 10


def test_the_window_is_clipped_at_the_end_of_the_recording(tmp_path):
    res = load_results(make_test(tmp_path))
    crack = find_crack(res, context=10_000)
    assert crack.start == 0 and crack.end == len(res.frames) - 1


def test_estimate_finds_the_sudden_change(tmp_path):
    res = load_results(make_test(tmp_path, jump_frame=305))
    pos = estimate_crack_frame(res)
    assert pos is not None and int(res.frames.index[pos]) == 305
    assert res.crack.contains(pos)


def test_without_full_rate_frames_it_names_the_frames_either_side(tmp_path):
    res = load_results(make_test(tmp_path, full_rate=False))
    crack = res.crack
    assert crack.source == "break"
    assert crack.n_frames == 1                                    # only the frame of the next load sample
    assert not crack.full_rate and "Save full-rate frames at the break" in crack.note
    assert estimate_crack_frame(res) is None


def test_no_break_falls_back_to_the_last_full_rate_window(tmp_path):
    res = load_results(make_test(tmp_path, break_detected=False))
    crack = res.crack
    assert crack.source == "high-speed window" and "No break detected" in crack.note
    assert int(res.frames.index[crack.first]) == 250 and int(res.frames.index[crack.last]) == 350


def test_images_deleted_keeps_the_rest(tmp_path):
    test = make_test(tmp_path, images=False)
    (test / "camera" / "images").rmdir()
    res = load_results(test)
    assert not res.has_images and res.images_deleted
    assert len(res.plots) == 2 and "Peak load" in res.summary_text
    assert estimate_crack_frame(res) is None


def test_a_camera_recording_without_a_test(tmp_path):
    test = make_test(tmp_path)
    res = load_results(test / "camera")
    assert res.test == test                                       # camera/ of a test: the test is found
    for f in ("data.csv", "meta.json", "summary.json", "summary.txt", "plot.png"):
        (test / f).unlink()
    res = load_results(test / "camera")
    assert res.test is None and res.run == test / "camera" and len(res.frames) > 0
    assert res.crack.source == "high-speed window"


def test_not_a_result_folder(tmp_path):
    with pytest.raises(ValueError):
        load_results(tmp_path)
