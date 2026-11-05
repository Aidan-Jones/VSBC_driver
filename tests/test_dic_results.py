"""The DIC tab's files, crack tips marked by hand and the checks before DIC runs (no Qt)."""

import math

import numpy as np
import pytest

from vsbc.camera import dic_results as dr
from vsbc.camera.postprocess import Options
from vsbc.camera.results import load_results
from vsbc.camera.roi import Roi, load_roi, rect_mask
from vsbc.camera.scripts import ScriptCheck
from vsbc.storage import DiskStatus
from tests.test_results import SHAPE, make_test

XS, YS = np.array([10.0, 30.0, 50.0]), np.array([10.0, 20.0, 30.0, 40.0])


def write_dic(run, frame, v_scale=1.0, unconverged=()):
    """A pyvale displacement CSV on a 3 × 4 grid: u = x / 100, v = y / 10 * v_scale."""
    out = run / "pyvale_results"
    out.mkdir(exist_ok=True)
    lines = ["ss_x,ss_y,u,v,m,conv"]
    k = 0
    for y in YS:
        for x in XS:
            conv = 0 if k in unconverged else 1
            lines.append(f"{x},{y},{x / 100},{y / 10 * v_scale},0,{conv}")
            k += 1
    (out / f"dic_results_frame_{frame:06d}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_strain(run, frame, eyy=1e-3):
    out = run / "pyvale_results"
    out.mkdir(exist_ok=True)
    lines = ["x,y,eps_00,eps_01,eps_10,eps_11"]
    for y in YS:
        for x in XS:
            lines.append(f"{x},{y},{-eyy / 3},{0.0},{0.0},{eyy * y / 40}")
    (out / f"strain_dic_results_frame_{frame:06d}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


ENV_OK = (True, "pyvale 2026.10.2, OpenCV 4.10")
DISK_OK = DiskStatus(None, 100e9, 50e9, 50e9, "ok")


def opts(**kw):
    base = dict(run_dic=True, run_strain=True, crack_tip_image=True, crack_tip_opening=True, make_video=False,
                overview_every=3, crack_every=1)
    base.update(kw)
    return Options(**base)


def test_files_grids_and_limits(tmp_path):
    run = make_test(tmp_path) / "camera"
    for f in (300, 301, 302):
        write_dic(run, f, v_scale=f - 299, unconverged=(0,))
        write_strain(run, f)
    assert list(dr.dic_files(run)) == [300, 301, 302] and list(dr.strain_files(run)) == [300, 301, 302]
    field = dr.displacement_field(dr.dic_files(run)[300], "v")
    assert field.grid.shape == (4, 3) and field.ok
    assert math.isnan(field.grid[0, 0]) and field.grid[3, 2] == pytest.approx(4.0)
    assert field.valid == pytest.approx(11 / 12)
    mag = dr.displacement_field(dr.dic_files(run)[300], "mag")
    assert mag.grid[3, 2] == pytest.approx(math.hypot(0.5, 4.0))
    assert dr.strain_columns(dr.strain_files(run)[300])[0] == "eps_11"
    strain = dr.strain_field(dr.strain_files(run)[300], "eps_11")
    assert strain.grid[3, 0] == pytest.approx(1e-3)
    assert dr.strain_field(dr.strain_files(run)[300], "eps_99") is None
    lo, hi = dr.color_limits([dr.displacement_field(p, "v") for p in dr.dic_files(run).values()])
    assert 0 < lo < hi
    lo, hi = dr.color_limits([strain], symmetric=True)
    assert lo == -hi
    idx, means = dr.mean_series(dr.dic_files(run), lambda p: dr.displacement_field(p, "v"))
    assert list(idx) == [300, 301, 302] and means[2] > means[0]


def test_manual_marks_round_trip_in_the_viewer_format(tmp_path):
    run = tmp_path
    dr.write_manual(run, {12: (100.0, 20.5), 3: (50.0, 21.0)}, {3: 0.25})
    text = (run / dr.MANUAL_FILE).read_text(encoding="utf-8").splitlines()
    assert text[0] == dr.MANUAL_HEADER and text[1].startswith("3,frame_000003.tif,50.0,21.0,0.250000")
    assert text[2].endswith(",nan")
    assert dr.read_manual(run) == {3: (50.0, 21.0), 12: (100.0, 20.5)}


def test_crack_length_follows_the_crack_direction():
    assert dr.crack_length(150.0, (100, 20), True) == 50.0
    assert dr.crack_length(150.0, (100, 20), False) == 0.0
    assert dr.crack_length(60.0, (100, 20), False) == 40.0
    assert math.isnan(dr.crack_length(60.0, None))


def test_tracker_lengths_and_speed(tmp_path):
    (tmp_path / "crack_tip_image.csv").write_text(
        "frame_index,time_s,crack_tip_x_px,crack_tip_y_px,crack_length_px\n1,0.1,10,5,0\n2,0.2,20,5,10\n",
        encoding="utf-8")
    (tmp_path / "crack_tip_opening.csv").write_text(
        "frame_index,time_s,tip_x_raw_px,tip_x_px,crack_length_px,crack_speed_px_s,threshold_px,crack_y_px\n"
        "1,0.1,10,10,0,nan,0.1,5\n2,0.2,22,22,12,120.0,0.1,5\n", encoding="utf-8")
    lengths = dr.tracker_lengths(tmp_path)
    assert list(lengths["image tracker"][1]) == [0.0, 10.0]
    assert dr.peak_crack_speed(tmp_path) == pytest.approx(120.0)


def _levels(checks):
    return {c.level for c in checks}


def test_validation_passes_on_a_good_test(tmp_path):
    res = load_results(make_test(tmp_path))
    checks = dr.validate(res, opts(), env=ENV_OK, disk=DISK_OK,
                         scripts=[ScriptCheck(n, tmp_path, True) for n in ("capture_dic_live.py",)])
    assert dr.can_run(checks), [c.text for c in checks]
    assert any("whole-test" in c.text for c in checks)


def test_validation_blocks_and_explains(tmp_path):
    res = load_results(make_test(tmp_path))
    # Still checking the environment / environment broken / already running / disk full / nothing ticked.
    assert not dr.can_run(dr.validate(res, opts(), env=None))
    bad_env = dr.validate(res, opts(), env=(False, "No module named 'pyvale'"))
    assert any(c.level == "error" and "pyvale" in c.text for c in bad_env)
    assert not dr.can_run(dr.validate(res, opts(), env=ENV_OK, busy=True))
    full = DiskStatus(None, 100e9, 99.5e9, 0.5e9, "critical")
    assert not dr.can_run(dr.validate(res, opts(), env=ENV_OK, disk=full))
    nothing = opts(run_dic=False, run_strain=False, crack_tip_image=False, crack_tip_opening=False)
    assert not dr.can_run(dr.validate(res, nothing, env=ENV_OK))
    # Strain without DIC results.
    checks = dr.validate(res, opts(run_dic=False), env=ENV_OK)
    assert any("need DIC results" in c.text for c in checks)
    # A broken script that is needed.
    broken = ScriptCheck("pyvale_dic_run.py", tmp_path, True, ["no top-level RUN_DIC"])
    assert not dr.can_run(dr.validate(res, opts(), env=ENV_OK, scripts=[broken]))
    assert dr.can_run(dr.validate(res, opts(run_strain=False), env=ENV_OK, scripts=[broken]))


def test_validation_roi_missing_offers_the_current_roi(tmp_path):
    test = make_test(tmp_path)
    (test / "camera" / "roi.npz").unlink()
    res = load_results(test)
    checks = dr.validate(res, opts(), env=ENV_OK)
    assert any(c.level == "error" and "Edit ROI" in c.text for c in checks)
    current = Roi(rect_mask(SHAPE, 2, 2, 60, 38), seed=(30, 20), notch=None)
    res.settings.update(height=SHAPE[0], width=SHAPE[1])
    checks = dr.validate(res, opts(), env=ENV_OK, current_roi=current)
    fix = [c for c in checks if c.fix == "use_current_roi"]
    assert fix and fix[0].level == "warn" and dr.can_run(checks)
    assert any("No notch tip" in c.text for c in checks)           # the current ROI has none
    wrong = Roi(rect_mask((10, 10), 1, 1, 9, 9), seed=(5, 5))
    checks = dr.validate(res, opts(), env=ENV_OK, current_roi=wrong)
    assert not dr.can_run(checks)
    assert load_roi(test / "camera" / "roi.npz") is None


def test_validation_counts_frames_already_done(tmp_path):
    test = make_test(tmp_path)
    run = test / "camera"
    for f in range(251, 260):
        write_dic(run, f)
    res = load_results(test)
    n_over, n_win, n_done = dr.frames_to_analyse(res, 3, 1, redo=False)
    assert n_win == 101 and n_done == 9                            # the full-rate window: frames 250..350
    assert dr.frames_to_analyse(res, 3, 1, redo=True)[2] == 0
    images_gone = load_results(make_test(tmp_path, images=False, name="T9"))
    checks = dr.validate(images_gone, opts(), env=ENV_OK)
    assert not dr.can_run(checks)


def test_check_camera_env_reports_a_missing_module():
    import sys

    ok, text = dr.check_camera_env(sys.executable)
    assert ok or "pyvale" in text or "cv2" in text
