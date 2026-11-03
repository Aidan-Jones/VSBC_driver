"""The clock sync against a synthetic recording whose true timing is known.

Three clocks with offsets and drift (Mega +300 ppm, camera -50 ppm), positive
random transport delays, Mega times in whole ms (protocol v2) or to 0.001 ms
(v3), a 400 fps camera and an 80 Hz load cell; frames saved 1:1 with the
samples plus one full-rate window.
"""

import csv
import json

import numpy as np
import pytest

from vsbc.camera.sync import (
    annotate_crack_csv, fit_latches, fit_lower_envelope, interpolate, latest_index, nearest_index, sync_test,
    video_schedule,
)

FPS, SPS, DURATION = 400.0, 80.0, 5.0
WINDOW = (1700, 1799)
RUN_START = 0.5


def mega_ms(tau):
    return (np.asarray(tau) * (1 + 300e-6) + 2.0) * 1000.0


def host_ns(tau):
    return (np.asarray(tau) + 100.0) * 1e9


def cam_ns(tau):
    return (np.asarray(tau) * (1 - 50e-6) + 7.0) * 1e9


def _write_recording(folder, rng, decimals=3):
    cam = folder / "camera"
    cam.mkdir(parents=True)
    tau_s = np.arange(0, DURATION, 1 / SPS)
    t_ms = np.floor(mega_ms(tau_s) * 10 ** decimals) / 10 ** decimals     # as the firmware prints it
    arrive = (host_ns(tau_s) + 1e6 + rng.exponential(2e6, tau_s.size)).astype(np.int64)
    pos, load = 1.0 * tau_s, 100.0 * tau_s
    with open(cam / "samples.csv", "w", newline="") as f:
        f.write("t_ms,host_ns,position_mm,load_N,state\n")
        for row in zip(t_ms, arrive, pos, load):
            f.write(f"{row[0]:.{decimals}f},{row[1]},{row[2]:.4f},{row[3]:.3f},R\n")
    tau_f = np.arange(int(DURATION * FPS)) / FPS
    saved = {0} | {int(i) for i in nearest_index(tau_f, tau_s)} | set(range(WINDOW[0], WINDOW[1] + 1))
    with open(cam / "frames.csv", "w", newline="") as f:
        f.write("frame_index,filename,saved,block_id,camera_timestamp_ns,pc_time_s,marker,host_ns,matched,full_rate\n")
        for i, tau in enumerate(tau_f):
            host = int(host_ns(tau) + 3e6 + rng.uniform(0, 3e5))
            f.write(f"{i},frame_{i:06d}.tif,{int(i in saved)},{i},{int(cam_ns(tau))},{tau:.6f},,{host},0,0\n")
    with open(cam / "camera_clock.csv", "w", newline="") as f:
        f.write("host_before_ns,camera_ns,host_after_ns\n")
        for tau in np.arange(0, DURATION, 0.5):
            f.write(f"{int(host_ns(tau) - 2e5)},{int(cam_ns(tau))},{int(host_ns(tau) + 2e5)}\n")
    (cam / "capture_settings.json").write_text(json.dumps({"full_rate_windows": [list(WINDOW)],
                                                           "exposure_us": 1000, "width": 640, "height": 160}))
    run = tau_s >= RUN_START
    t0 = float(t_ms[run][0])
    (folder / "meta.json").write_text(json.dumps({"mega_t0_ms": t0, "start_position_mm": float(pos[run][0]),
                                                  "units": {"force": "N", "length": "mm"}}))
    with open(folder / "data.csv", "w", newline="") as f:
        f.write("time_s,position_mm,extension_mm,load_N\n")
        for t, p, w in zip(t_ms[run], pos[run], load[run]):
            f.write(f"{(t - t0) / 1000:.4f},{p:.4f},{p - pos[run][0]:.4f},{w:.3f}\n")
    truth = (mega_ms(tau_f) - t0) / 1000.0
    return truth, tau_s, saved


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_lower_envelope_follows_the_fastest_arrivals():
    rng = np.random.default_rng(1)
    tau = np.arange(0, 60, 1 / SPS)
    sent = (mega_ms(tau) * 1e6).astype(np.int64)
    arrived = (host_ns(tau) + 1e6 + rng.exponential(3e6, tau.size)).astype(np.int64)
    fit = fit_lower_envelope(sent, arrived)
    assert fit.ppm == pytest.approx(-300, abs=10)          # host per Mega tick: 1/(1+300e-6)
    error_ms = (fit(sent) - host_ns(tau)) / 1e6
    assert np.abs(error_ms - 1.0).max() < 0.3              # the minimum delay (1 ms) is what remains


def test_latch_fit():
    tau = np.arange(0, 30, 2.0)
    latches = np.column_stack([host_ns(tau) - 1e5, cam_ns(tau), host_ns(tau) + 1e5]).astype(np.int64)
    fit = fit_latches(latches)
    assert fit.ppm == pytest.approx(50, abs=1)
    assert np.abs(fit(cam_ns(tau).astype(np.int64)) - host_ns(tau)).max() < 1e3


def test_helpers():
    t = np.array([0.0, 1.0, 2.0])
    assert list(nearest_index(t, [0.4, 0.6, 5.0, -1.0])) == [0, 1, 2, 0]
    assert list(latest_index(t, [0.5, 1.0, -0.1])) == [0, 1, -1]
    out = interpolate([0.5, 3.0], t, np.array([0.0, 10.0, 20.0]))
    assert out[0] == pytest.approx(5.0) and np.isnan(out[1])
    times, pick = video_schedule(np.array([0.0, 0.04, 0.1]), 30.0, 0.1)
    assert times.size == 4 and list(pick) == [0, 1, 1, 2]


@pytest.mark.parametrize("decimals, max_error_ms, spread_ms", [(0, 1.5, 0.6), (3, 1.2, 0.3)])
def test_sync_puts_frames_on_the_data_csv_clock(tmp_path, decimals, max_error_ms, spread_ms):
    truth, tau_s, saved = _write_recording(tmp_path, np.random.default_rng(7), decimals)
    summary = sync_test(tmp_path)
    frames = _rows(tmp_path / "camera" / "frames.csv")
    t = np.array([float(r["time_s"]) for r in frames])
    error_ms = (t - truth) * 1000
    # About -1 ms is the minimum transport delay, which no fit can tell from an offset (the
    # tap test's load_delay_ms takes it up). Whole-ms Mega times add up to 1 ms of spread.
    assert np.abs(error_ms).max() < max_error_ms and np.ptp(error_ms) < spread_ms
    assert summary["frames_saved"] == len(saved)

    # Every data.csv row got the frame saved for it, within half a camera period.
    data = _rows(tmp_path / "data.csv")
    dt = np.array([float(r["frame_dt_ms"]) for r in data])
    assert np.abs(dt).max() < 1000 / FPS / 2 + 1.5
    assert summary["data_csv_pairing"]["rows"] == len(data)
    assert all(int(r["frame"]) in saved for r in data)

    # Load: interpolated to the frame's time, but the latest reading inside the full-rate window.
    by_index = {int(r["frame_index"]): r for r in frames}
    inside = by_index[WINDOW[0] + 37]
    outside = next(r for i, r in by_index.items() if r["saved"] == "1" and i > 400 and not WINDOW[0] <= i <= WINDOW[1])
    assert inside["load_method"] == "latest" and outside["load_method"] == "interp"
    tau_out = int(outside["frame_index"]) / FPS
    assert float(outside["load_N"]) == pytest.approx(100 * tau_out, abs=0.5)
    tau_in = (WINDOW[0] + 37) / FPS
    held = 100 * tau_s[tau_s <= tau_in + 0.0015][-1]
    assert float(inside["load_N"]) == pytest.approx(held, abs=1.3)
    assert float(outside["extension_mm"]) == pytest.approx(tau_out - RUN_START, abs=0.02)
    assert by_index[1]["load_N"] == ""                                # not saved: no load

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["camera"]["frames_saved"] == len(saved)

    # Syncing again does not duplicate the columns.
    sync_test(tmp_path)
    assert list(_rows(tmp_path / "data.csv")[0]).count("frame") == 1


def test_crack_tip_csv_gets_test_time_and_load(tmp_path):
    _write_recording(tmp_path, np.random.default_rng(3))
    sync_test(tmp_path)
    crack = tmp_path / "camera" / "crack_tip_image.csv"
    crack.write_text("frame_index,time_s,crack_tip_x_px,crack_tip_y_px,crack_length_px\n"
                     f"{WINDOW[0]},0.1,10,20,0\n")
    assert annotate_crack_csv(crack, tmp_path / "camera")
    row = _rows(crack)[0]
    assert float(row["test_time_s"]) == pytest.approx(WINDOW[0] / FPS * (1 + 300e-6) - RUN_START, abs=0.01)
    assert row["load_N"] != ""
