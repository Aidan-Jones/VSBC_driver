import csv

import numpy as np
import pytest

from vsbc.analysis import REVERSED_TENSION_NOTE, analyze, tension_reads_negative, tension_threshold_n
from vsbc.config import Config
from vsbc.protocol import Sample
from vsbc.recorder import CSV_COLUMNS, TestRecorder, unit_columns
from vsbc.units import Units, clear_saved_units, effective_units, save_units, units_from_dict


def test_conversions_round_trip():
    u = Units("lbf", "in")
    assert u.force_from_n(4.4482216152605) == pytest.approx(1.0)
    assert u.force_to_n(u.force_from_n(123.4)) == pytest.approx(123.4)
    assert u.length_from_mm(25.4) == pytest.approx(1.0)
    assert u.area_from_mm2(645.16) == pytest.approx(1.0)
    assert u.stiffness_from_n_per_mm(1.0) == pytest.approx(25.4 / 4.4482216152605)
    assert u.stress_label == "psi" and u.stress_from_mpa(1.0) == pytest.approx(145.0377, rel=1e-6)
    assert u.energy_label == "in·lbf" and u.energy_from_j(1.0) == pytest.approx(8.8507458, rel=1e-6)
    assert Units("N", "m").length_from_mm(1500) == pytest.approx(1.5)
    assert Units().is_default and not u.is_default
    assert u.format_force(100.0) == "22.481 lbf"
    assert u.format_length(float("nan")) == "—"


def test_bad_units_are_rejected():
    with pytest.raises(ValueError):
        Units("kgf", "mm")
    assert units_from_dict({"force": "kgf"}) == Units()
    assert units_from_dict({"length": "cm"}) == Units("N", "cm")


def test_preferences_override_the_config(tmp_path):
    cfg = Config(output_dir=tmp_path)
    cfg.units.length = "cm"
    assert effective_units(cfg) == Units("N", "cm")
    save_units(cfg, Units("lbf", "in"))
    assert effective_units(cfg) == Units("lbf", "in")
    clear_saved_units(cfg)
    assert effective_units(cfg) == Units("N", "cm")


def _summary():
    ext = np.linspace(0, 2, 200)
    load = np.where(ext < 1.5, 400 * ext, 0.0)
    return analyze(ext / 5, ext, load, area_mm2=10.0, gauge_length_mm=50.0, break_drop_pct=40, break_min_n=5)


def test_summary_text_in_other_units():
    s = _summary()
    default = s.to_text()
    assert "N/mm" in default and " mm" in default and "MPa" in default and " J" in default
    text = s.to_text(Units("lbf", "in"))
    assert "lbf/in" in text and "psi" in text and "in·lbf" in text and " N " not in text
    peak_lbf = Units("lbf", "in").force_from_n(s.peak_load_n)
    assert f"{peak_lbf:.3f} lbf" in text


def test_recorder_adds_unit_columns(tmp_path):
    units = Units("lbf", "in")
    rec = TestRecorder(tmp_path, "U1", {}, area_mm2=10.0, gauge_length_mm=50.0, units=units)
    for i in range(20):
        rec.add(Sample(1000 + 12 * i, 5.0 + 0.0254 * i, 4.4482216152605 * i, 1000 * i, "R"))
    rec.finish("DONE")
    rows = list(csv.DictReader(open(rec.folder / "data.csv", encoding="utf-8")))
    assert list(rows[0]) == CSV_COLUMNS + unit_columns(units)
    assert unit_columns(units) == ["position_in", "extension_in", "load_lbf", "stress_psi"]
    assert float(rows[-1]["load_lbf"]) == pytest.approx(19.0)
    assert float(rows[-1]["extension_in"]) == pytest.approx(0.019, abs=1e-5)
    assert float(rows[-1]["load_N"]) == pytest.approx(19 * 4.4482216152605, abs=1e-3)   # N columns stay
    text = (rec.folder / "summary.txt").read_text(encoding="utf-8")
    assert "lbf" in text and "Units: lbf, in" in text
    assert rec.meta["units"] == {"force": "lbf", "length": "in"}
    assert rec.meta["mega_t0_ms"] == 1000 and rec.meta["start_position_mm"] == pytest.approx(5.0)


def test_default_units_keep_the_columns(tmp_path):
    rec = TestRecorder(tmp_path, "U2", {})
    rec.add(Sample(0, 0.0, 1.0, 10, "R"))
    rec.finish("DONE")
    assert list(csv.DictReader(open(rec.folder / "data.csv", encoding="utf-8")))[0].keys() == set(CSV_COLUMNS)


def test_reversed_tension_is_detected():
    ext = np.linspace(0, 1, 50)
    assert tension_reads_negative(-200 * ext, ext)
    assert not tension_reads_negative(200 * ext, ext)
    # A little compression before the specimen takes up load is fine.
    assert not tension_reads_negative(np.concatenate([[-3, -3, -3, -3, -3, -3], 200 * ext]),
                                      np.concatenate([np.zeros(6), ext]))
    # Noise around zero is not a reversed sign.
    assert not tension_reads_negative(np.random.default_rng(0).normal(0, 0.5, 50), ext)
    assert tension_threshold_n(500.0) == pytest.approx(5.0)
    assert tension_threshold_n(None) == pytest.approx(2.0)


def test_recorder_warns_about_reversed_tension(tmp_path):
    rec = TestRecorder(tmp_path, "NEG", {}, capacity_n=500.0)
    for i in range(30):
        rec.add(Sample(1000 + 12 * i, 0.01 * i, -10.0 * i, 0, "R"))
    rec.finish("DONE")
    assert REVERSED_TENSION_NOTE in rec.warnings
    assert "sign is reversed" in (rec.folder / "summary.txt").read_text(encoding="utf-8")
