import json
import time

import pytest

from vsbc.calibration import (
    LoadPoint, TravelTrial, apply_load_fit, collect_raw, fit_load_calibration, latest_record,
    mass_to_newtons, parse_load, save_record, travel_correction,
)


def _points(scale, offset, forces):
    return [LoadPoint(f, offset + scale * f, 5.0, 40) for f in forces]


def test_fit_recovers_scale_and_offset():
    fit = fit_load_calibration(_points(4194.304, 84213, [0, 9.81, 49.03, 98.07]), capacity_n=500)
    assert fit.scale == pytest.approx(4194.304)
    assert fit.offset == pytest.approx(84213)
    assert fit.r2 == pytest.approx(1.0)
    assert fit.max_residual_n == pytest.approx(0, abs=1e-6)
    assert fit.max_residual_pct_fs == pytest.approx(0, abs=1e-6)


def test_fit_handles_negative_scale():
    fit = fit_load_calibration(_points(-2000.0, -5000, [0, 10, 20]))
    assert fit.scale == pytest.approx(-2000.0)


def test_fit_needs_two_distinct_loads():
    with pytest.raises(ValueError):
        fit_load_calibration(_points(1000, 0, [0]))
    with pytest.raises(ValueError):
        fit_load_calibration(_points(1000, 0, [5, 5]))


def test_units():
    assert mass_to_newtons(1, "kg") == pytest.approx(9.80665)
    assert mass_to_newtons(500, "g") == pytest.approx(4.903325)
    assert parse_load("2 kg") == pytest.approx(19.6133)
    assert parse_load("10N") == pytest.approx(10.0)
    assert parse_load("1.5 kN") == pytest.approx(1500.0)
    assert parse_load("7") == pytest.approx(7.0)
    with pytest.raises(ValueError):
        parse_load("heavy")
    with pytest.raises(ValueError):
        mass_to_newtons(1, "stone")


def test_travel_correction():
    r = travel_correction(80.0, [TravelTrial(20.0, 19.8)])
    assert r.ratio == pytest.approx(0.99)
    assert r.new_steps_per_mm == pytest.approx(80.0 / 0.99)
    assert r.warnings == []


def test_travel_correction_flags_dip_switch_mismatch():
    # Drivers left at 25000 pulses/rev: 20 mm commanded -> 0.32 mm measured.
    r = travel_correction(80.0, [TravelTrial(20.0, 0.32)])
    text = " ".join(r.warnings)
    assert "DIP" in text and "25000" in text


def test_travel_correction_flags_inconsistent_trials():
    r = travel_correction(80.0, [TravelTrial(20.0, 19.8), TravelTrial(20.0, 20.2)])
    assert any("disagree" in w for w in r.warnings)


def test_records(tmp_path):
    fit = fit_load_calibration(_points(1000, 10, [0, 10]))
    path = save_record(tmp_path, "load", {"fit": fit, "points": _points(1000, 10, [0, 10])})
    data = json.loads(path.read_text())
    assert data["kind"] == "load" and data["fit"]["scale"] == pytest.approx(1000)
    found = latest_record(tmp_path, "load")
    assert found and found[0] == path
    assert latest_record(tmp_path, "travel") is None


def test_multi_point_calibration_against_simulator(machine, sim):
    sim.set_external_load(0.0)
    machine.tare()
    points = []
    for force in (0.0, 49.03, 98.07):
        sim.set_external_load(force)
        time.sleep(0.1)
        mean, std, n = collect_raw(machine, seconds=0.5)
        points.append(LoadPoint(force, mean, std, n))
    sim.set_external_load(0.0)
    fit = fit_load_calibration(points, capacity_n=500)
    assert fit.scale == pytest.approx(sim.true_scale, rel=0.005)
    assert fit.r2 > 0.9999
    apply_load_fit(machine, fit)
    assert machine.refresh_settings()["load_scale"] == pytest.approx(fit.scale, rel=1e-4)
