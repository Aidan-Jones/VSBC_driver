import csv
import json
import threading
import time

import numpy as np
import pytest

from vsbc.analysis import load_csv
from vsbc.procedures import PreflightError, PullTestRunner, PullTestSpec
from vsbc.protocol import Sample
from vsbc.recorder import CSV_COLUMNS, TestRecorder


def _spec(**kw):
    values = dict(specimen_id="A1", rate_mm_min=300.0, max_extension_mm=5.0,
                  break_drop_pct=40.0, break_min_n=5.0, area_mm2=10.0, gauge_length_mm=50.0)
    values.update(kw)
    return PullTestSpec(**values)


def test_pull_test_to_break_writes_all_files(machine, config):
    machine.zero()
    machine.set("max_load", 450.0)
    samples = []
    runner = PullTestRunner(machine, _spec(), config, on_sample=lambda s, e: samples.append(e))
    assert runner.preflight() == []
    result = runner.run()

    assert result.stop_reason == "BREAK" and not result.aborted
    folder = result.folder
    for name in ("data.csv", "meta.json", "summary.json", "summary.txt", "plot.png", "serial.log"):
        assert (folder / name).is_file(), name
    assert folder.parent.parent == config.tests_dir

    s = result.summary
    assert s.peak_load_n == pytest.approx(400, rel=0.05)
    assert s.break_detected
    assert s.stiffness_n_per_mm == pytest.approx(400, rel=0.1)
    assert s.uts_mpa == pytest.approx(40, rel=0.05)

    cols = load_csv(folder / "data.csv")
    assert list(cols) == CSV_COLUMNS
    # The first sample comes up to about one sample period (12.5 ms = 0.06 mm at 5 mm/s) after the start.
    assert 0 <= cols["extension_mm"][0] < 0.15
    assert (np.diff(cols["extension_mm"]) >= 0).all()
    assert len(samples) == len(cols["time_s"]) > 10

    meta = json.loads((folder / "meta.json").read_text())
    assert meta["status"] == "complete" and meta["stop_reason"] == "BREAK"
    assert meta["specimen"]["specimen_id"] == "A1"
    assert meta["settings"]["break_drop"] == pytest.approx(40.0)
    assert "RUN" in (folder / "serial.log").read_text()


def test_operator_stop_keeps_data(machine, config, sim):
    from vsbc.sim import Specimen

    machine.zero()
    sim.remount_specimen(Specimen(extension_at_peak_mm=40.0, break_extension_mm=45.0))
    runner = PullTestRunner(machine, _spec(rate_mm_min=60.0, max_extension_mm=5.0), config)
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("r", runner.run()))
    worker.start()
    time.sleep(0.8)
    runner.request_stop()
    worker.join(5)
    result = box["r"]
    assert result.stop_reason == "STOP" and not result.aborted
    assert result.summary.n_samples > 10


def test_preflight_lists_problems(machine, config):
    runner = PullTestRunner(machine, _spec(specimen_id=" ", rate_mm_min=10000.0, max_extension_mm=500.0), config)
    with pytest.raises(PreflightError) as e:
        runner.preflight()
    text = str(e.value)
    assert "specimen ID" in text and "referenced" in text and "Rate" in text and "travel limit" in text


def test_preflight_warns_without_load_limits(machine, config):
    machine.zero()
    warnings = PullTestRunner(machine, _spec(break_drop_pct=0.0), config).preflight()
    assert any("max_load" in w for w in warnings)
    assert any("Break detection is off" in w for w in warnings)


def test_run_error_is_recorded(machine, config):
    machine.zero()
    machine.set("max_pos", 1.0)       # RUN of 5 mm now fails with ERR LIMIT
    result = PullTestRunner(machine, _spec(), config).run()
    assert result.aborted and "LIMIT" in result.error
    meta = json.loads((result.folder / "meta.json").read_text())
    assert meta["status"] == "aborted"


def test_recorder_blank_stress_without_geometry(tmp_path):
    rec = TestRecorder(tmp_path, "no geometry/../x", {"test": "unit"})
    assert rec.folder.name.endswith("_no_geometry_.._x")   # one safe folder name, no path parts
    for i in range(20):
        rec.add(Sample(1000 + 12 * i, 5.0 + 0.01 * i, 10.0 * i, 1000 * i, "R"))
    summary = rec.finish("DONE")
    rows = list(csv.DictReader(open(rec.folder / "data.csv", encoding="utf-8")))
    assert rows[0]["stress_MPa"] == "" and rows[0]["strain"] == ""
    assert float(rows[-1]["extension_mm"]) == pytest.approx(0.19)
    assert summary.peak_load_n == pytest.approx(190)
    assert rec.folder.parent.parent == tmp_path
