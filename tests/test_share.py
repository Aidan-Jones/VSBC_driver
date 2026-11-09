"""The one-zip-per-test upload folder (vsbc.share) and its post-processing job."""

import json
import zipfile

from vsbc.camera import jobs
from vsbc.camera.postprocess import Options, PostProcessor
from vsbc.share import build_upload_zip, upload_path
from vsbc.storage import list_outputs


def _make_test(out, date="2026-11-09", name="101500_A1", n_frames=301, window=(250, 300)):
    folder = out / "tests" / date / name
    cam = folder / "camera"
    (cam / "images").mkdir(parents=True)
    (cam / "pyvale_results").mkdir()
    (cam / "plots").mkdir()
    (folder / "data.csv").write_text("time_s,load_N\n0,0\n", encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    (folder / "plot.png").write_bytes(b"\x89PNG fake")
    (folder / "video.mp4").write_bytes(b"fake mp4" * 100)
    (cam / "frames.csv").write_text("frame_index,saved\n0,1\n", encoding="utf-8")
    (cam / "capture_settings.json").write_text(json.dumps({"full_rate_windows": [list(window)]}),
                                               encoding="utf-8")
    (cam / "postprocess_status.json").write_text("{}", encoding="utf-8")
    (cam / "pyvale_results" / "dic_results_frame_000010.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (cam / "plots" / "pyvale_dic_run_01.png").write_bytes(b"\x89PNG fake")
    for i in range(n_frames):
        (cam / "images" / f"frame_{i:06d}.tif").write_bytes(bytes(64))
    return folder


def test_zip_keeps_results_reference_and_break_frames(tmp_path):
    out = tmp_path / "Outputs"
    folder = _make_test(out)
    path = build_upload_zip(folder, out)
    assert path == out / "upload" / "2026-11-09" / "101500_A1.zip" == upload_path(folder, out)
    assert not list(path.parent.glob("*.part"))
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        frames = {n for n in names if "/images/" in n}
        assert frames == {f"101500_A1/camera/images/frame_{i:06d}.tif" for i in [0, *range(250, 301)]}
        for wanted in ("data.csv", "meta.json", "plot.png", "video.mp4", "camera/frames.csv",
                       "camera/capture_settings.json", "camera/pyvale_results/dic_results_frame_000010.csv",
                       "camera/plots/pyvale_dic_run_01.png"):
            assert f"101500_A1/{wanted}" in names
        assert "101500_A1/camera/postprocess_status.json" not in names
        assert zf.getinfo("101500_A1/video.mp4").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("101500_A1/camera/images/frame_000000.tif").compress_type == zipfile.ZIP_DEFLATED

    # Remaking it replaces it, and the Outputs listing shows it.
    (folder / "summary.txt").write_text("peak 100 N", encoding="utf-8")
    build_upload_zip(folder, out)
    with zipfile.ZipFile(path) as zf:
        assert "101500_A1/summary.txt" in zf.namelist()
    assert [e.has_upload for e in list_outputs(out)] == [True]


def test_zip_of_a_test_without_camera(tmp_path):
    out = tmp_path / "Outputs"
    folder = out / "tests" / "2026-11-09" / "120000_B2"
    folder.mkdir(parents=True)
    (folder / "data.csv").write_text("time_s\n0\n", encoding="utf-8")
    with zipfile.ZipFile(build_upload_zip(folder, out)) as zf:
        assert zf.namelist() == ["120000_B2/data.csv"]


def test_package_job_is_queued_last_and_runs(tmp_path, config):
    folder = _make_test(config.output_dir)
    run = folder / "camera"
    pp = PostProcessor(config)
    queued = pp.jobs_for(run, folder, Options(run_dic=False, run_strain=False, crack_tip_image=False,
                                              crack_tip_opening=False, make_video=False), {})
    assert queued[-1].kind == "package" and queued[-1].needs == ()
    assert jobs.main(queued[-1].args[2:]) == 0
    assert upload_path(folder, config.output_dir).is_file()

    config.storage.upload_zips = False
    assert not any(j.kind == "package" for j in pp.jobs_for(run, folder, Options(make_video=False), {}))
