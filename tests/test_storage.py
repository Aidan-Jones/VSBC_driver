import json
import os
import time

import pytest

from vsbc.storage import (
    GB, IMAGES_DELETED_FILE, copy_outputs, delete_entry, delete_images, delete_old_logs, disk_status, human_size,
    list_outputs, remaining_tests,
)


def _make_test(root, day="2026-11-02", name="153012_A1", images=3, video=True):
    folder = root / "tests" / day / name
    (folder / "camera" / "images").mkdir(parents=True)
    (folder / "camera" / "pyvale_results").mkdir()
    (folder / "data.csv").write_text("time_s,load_N\n0,1\n")
    (folder / "meta.json").write_text(json.dumps({"started": f"{day}T15:30:12", "status": "complete",
                                                  "stop_reason": "BREAK", "specimen": {"specimen_id": "A1"}}))
    (folder / "camera" / "frames.csv").write_text("frame_index,saved\n0,1\n")
    for i in range(images):
        (folder / "camera" / "images" / f"frame_{i:06d}.tif").write_bytes(b"x" * 1000)
    (folder / "camera" / "pyvale_results" / "dic_results_frame_000001.csv").write_text("a,b\n1,2\n")
    if video:
        (folder / "video.mp4").write_bytes(b"v" * 500)
    return folder


def test_disk_levels(tmp_path):
    ok = disk_status(tmp_path, warn_free_gb=0.0, critical_free_gb=0.0)
    assert ok.level == "ok" and ok.free > 0 and 0 <= ok.used_fraction <= 1
    assert disk_status(tmp_path, warn_free_gb=1e9, critical_free_gb=0.0).level == "low"
    assert disk_status(tmp_path, warn_free_gb=1e9, critical_free_gb=1e9).level == "critical"
    # A folder that does not exist yet is measured on its drive.
    assert disk_status(tmp_path / "not" / "yet").total == ok.total
    assert remaining_tests(10 * GB, 2 * GB) == 4
    assert human_size(1536) == "1.5 kB"


def test_listing_sizes_and_flags(tmp_path):
    _make_test(tmp_path)
    rec = tmp_path / "camera" / "2026-11-02" / "160000_noise"
    (rec / "images").mkdir(parents=True)
    (rec / "frames.csv").write_text("frame_index\n")
    (rec / "images" / "frame_000000.tif").write_bytes(b"x" * 100)
    (tmp_path / "camera" / "last_roi.npz").write_bytes(b"roi")      # a file, not a recording
    entries = list_outputs(tmp_path)
    assert [e.kind for e in entries] == ["recording", "test"]       # newest first
    test = entries[1]
    assert test.name == "A1" and test.stop_reason == "BREAK"
    assert test.n_images == 3 and test.images_size == 3000
    assert test.n_dic == 1 and test.has_video and test.has_camera
    assert test.size >= 3000 + 500


def test_copy_with_and_without_images(tmp_path):
    out = tmp_path / "out"
    _make_test(out)
    entries = list_outputs(out)
    usb = tmp_path / "usb"
    usb.mkdir()
    seen = []
    result = copy_outputs(entries, out, usb, include_images=False, progress=lambda d, t, n: seen.append((d, t)))
    target = usb / "VSBC_outputs" / "tests" / "2026-11-02" / "153012_A1"
    assert (target / "data.csv").is_file() and (target / "video.mp4").is_file()
    assert not (target / "camera" / "images").exists()
    assert not result.errors and not result.cancelled and seen[-1][0] == seen[-1][1]
    copy_outputs(entries, out, usb, include_images=True)
    assert len(list((target / "camera" / "images").iterdir())) == 3


def test_delete_images_then_the_test(tmp_path):
    folder = _make_test(tmp_path)
    entry = list_outputs(tmp_path)[0]
    assert delete_images(entry) == 3000
    assert not (folder / "camera" / "images").exists()
    assert (folder / "camera" / IMAGES_DELETED_FILE).is_file()
    assert (folder / "data.csv").is_file() and (folder / "video.mp4").is_file()
    entry = list_outputs(tmp_path)[0]
    assert entry.images_deleted and entry.n_images == 0
    delete_entry(entry, tmp_path)
    assert not folder.exists() and not folder.parent.exists()      # the empty date folder goes too
    with pytest.raises(ValueError):
        delete_entry(entry.__class__(tmp_path.parent, "test", "", "x"), tmp_path)


def test_old_logs_are_deleted(tmp_path):
    old, new, app = tmp_path / "2025-01-01_serial.log", tmp_path / "today_serial.log", tmp_path / "vsbc.log"
    for path in (old, new, app):
        path.write_text("x" * 10)
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    os.utime(app, (past, past))
    assert delete_old_logs(tmp_path, 30) == (1, 10)
    assert not old.exists() and new.exists() and app.exists()
