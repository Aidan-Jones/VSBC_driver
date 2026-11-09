"""The camera process (with the simulated camera) recording pull tests on the simulated Mega."""

import csv
import json
import threading
import time

import numpy as np
import pytest

from vsbc.camera.client import CameraClient
from vsbc.camera.roi import Roi, rect_mask
from vsbc.camera.sync import sync_camera, sync_test
from vsbc.machine import Machine
from vsbc.procedures import PullTestRunner, PullTestSpec
from vsbc.sim import Specimen

FRAME_PERIOD_MS = 10.0              # the simulated camera runs at 100 fps
OPTIONS = dict(mode="match", divisor=1, buffer_s=1.0, break_s=0.3, settle_s=0.2, dump_on_break=True,
               load_delay_ms=0.0)
ROI = Roi(rect_mask((160, 640), 20, 20, 620, 140), seed=(320, 80), notch=(25, 80))


def _start(config, machine) -> CameraClient:
    events = []
    cam = CameraClient(config, machine, on_event=events.append, sim=True)
    cam.events = events
    cam.start()
    deadline = time.monotonic() + 30
    while cam.state not in ("preview", "error") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cam.state == "preview", cam.error
    return cam


@pytest.fixture
def camera(config, machine):
    cam = _start(config, machine)
    yield cam
    cam.stop()


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _pull(machine, config, camera, spec, during=None):
    machine.zero()
    before, after = camera.test_hooks(OPTIONS, ROI, None)
    runner = PullTestRunner(machine, spec, config, before_run=before, after_run=after)
    camera.runner = runner
    if during is not None:
        def watch():
            deadline = time.monotonic() + 10
            while camera.state != "recording" and time.monotonic() < deadline:
                time.sleep(0.01)
            during(runner)
        threading.Thread(target=watch, daemon=True).start()
    result = runner.run()
    assert camera.wait_finished(30)
    camera.runner = None
    return result


def test_pull_test_is_recorded_one_frame_per_sample(machine, config, camera):
    spec = PullTestSpec("CAM", rate_mm_min=60.0, max_extension_mm=5.0, break_drop_pct=40.0, break_min_n=5.0)
    result = _pull(machine, config, camera, spec)
    assert result.stop_reason == "BREAK" and not result.aborted
    cam_dir = result.folder / "camera"
    summary = sync_test(result.folder)

    frames = _rows(cam_dir / "frames.csv")
    saved = [f for f in frames if f["saved"] == "1"]
    assert frames[0]["saved"] == "1" and (cam_dir / "images" / "frame_000000.tif").is_file()
    assert all((cam_dir / "images" / f["filename"]).is_file() for f in saved)
    samples = _rows(cam_dir / "samples.csv")
    matched = [f for f in frames if f["matched"] == "1"]
    # One frame per load sample. (The simulated Mega's sample timer is irregular on Windows, where
    # waits round to ~16 ms, so two samples can fall closer together than a frame and share it.)
    assert len(matched) >= 0.9 * len({s["t_ms"] for s in samples})

    settings = json.loads((cam_dir / "capture_settings.json").read_text())
    assert len(settings["full_rate_windows"]) == 1           # the last break_s of the RAM buffer
    assert settings["break_seconds"] == 0.3
    start, end = settings["full_rate_windows"][0]
    # 0.3 s at 100 fps, not the whole 1 s ring (the simulated camera's timing is loose on Windows)
    assert 15 <= end - start + 1 <= 45
    window = [f for f in frames if start <= int(f["frame_index"]) <= end]
    assert all(f["saved"] == "1" and f["load_method"] in ("latest", "") for f in window)
    assert (cam_dir / "roi.npz").is_file()

    # data.csv rows each name a frame taken at the same moment.
    data = _rows(result.folder / "data.csv")
    assert summary["data_csv_pairing"]["rows"] == len(data)
    assert summary["data_csv_pairing"]["frame_dt_ms_p95"] < 1.5 * FRAME_PERIOD_MS
    assert all(r["frame"] for r in data)
    # The camera stopped with the trial.
    last_frame_t = max(float(f["time_s"]) for f in frames)
    assert abs(last_frame_t - float(data[-1]["time_s"])) < 0.5
    meta = json.loads((result.folder / "meta.json").read_text())
    assert meta["camera"]["frames_saved"] == len(saved)


def test_high_speed_saves_every_frame_until_the_end(machine, config, camera, sim):
    sim.remount_specimen(Specimen(extension_at_peak_mm=40.0, break_extension_mm=45.0))
    spec = PullTestSpec("HS", rate_mm_min=60.0, max_extension_mm=5.0, break_drop_pct=40.0, break_min_n=5.0)

    def during(runner):
        time.sleep(0.6)
        camera.send("high_speed", on=True)
        time.sleep(0.6)
        runner.request_stop()

    result = _pull(machine, config, camera, spec, during)
    assert result.stop_reason == "STOP"
    settings = json.loads((result.folder / "camera" / "capture_settings.json").read_text())
    assert settings["window_labels"] == ["high speed"]
    start, end = settings["full_rate_windows"][0]
    assert end - start > 0.6 * 100                           # the RAM buffer plus every frame since
    saved = {int(f["frame_index"]) for f in _rows(result.folder / "camera" / "frames.csv") if f["saved"] == "1"}
    assert set(range(start, end + 1)) <= saved
    assert any(e.get("ev") == "high_speed" and e.get("on") for e in camera.events)


def test_camera_stopping_stops_the_test(machine, config, camera, sim):
    sim.remount_specimen(Specimen(extension_at_peak_mm=40.0, break_extension_mm=45.0))
    spec = PullTestSpec("STOPS", rate_mm_min=60.0, max_extension_mm=5.0, break_drop_pct=40.0, break_min_n=5.0)

    def during(runner):
        time.sleep(0.5)
        camera._dispatch({"ev": "stopped_itself", "reason": "the disk is full"}, None)

    result = _pull(machine, config, camera, spec, during)
    assert result.stop_reason == "STOP"
    assert any("camera stopped recording (the disk is full)" in w for w in result.warnings)
    assert result.summary.max_extension_mm < 3.0


def test_recording_without_load_samples_falls_back(config, tmp_path):
    machine = Machine(config)                               # no Mega: no load samples
    cam = _start(config, machine)
    try:
        folder = cam.manual_folder("noise")
        cam.begin_recording(folder, dict(OPTIONS, max_s=60), ROI, None, test=False)
        time.sleep(2.5)
        cam.stop_recording("done")
        assert cam.wait_finished(30)
        summary = sync_camera(folder)
        frames = _rows(folder / "frames.csv")
        saved = [int(f["frame_index"]) for f in frames if f["saved"] == "1"]
        assert len(saved) > 5 and not (folder / "samples.csv").read_text().count("\n") > 1
        assert float(frames[0]["time_s"]) == pytest.approx(0.0, abs=1e-6)
        assert summary["mega_to_pi"] is None and summary["camera_to_pi"]["method"] == "timestamp latch"
        times = np.array([float(f["time_s"]) for f in frames])
        assert np.all(np.diff(times) > 0)
    finally:
        cam.stop()


def test_restart_keeps_the_new_camera_process(config, machine):
    def senders():
        return sum(t.name == "camera-sender" and t.is_alive() for t in threading.enumerate())

    before = senders()
    cam = _start(config, machine)
    try:
        first = cam._proc
        for _ in range(2):
            cam.restart()
            deadline = time.monotonic() + 30
            while cam.state != "preview" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert cam.state == "preview", cam.error
            time.sleep(0.3)       # a late "lost" from the old process would land here and must change nothing
            assert cam.state == "preview" and cam._proc is not None and cam._proc.poll() is None
            assert senders() == before + 1
        assert first.poll() is not None and cam._proc is not first
    finally:
        cam.stop()
    assert senders() == before
