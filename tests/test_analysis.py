import numpy as np
import pytest

from vsbc.analysis import analyze, find_break
from vsbc.sim import Specimen
from tests.conftest import FAST_SPECIMEN


def _curve(step=0.005):
    spec = Specimen(**FAST_SPECIMEN)
    ext = np.arange(0.0, 1.8, step)
    load = np.array([spec.load(e) for e in ext])
    time_s = ext / 5.0
    return time_s, ext, load


def test_peak_break_and_stiffness():
    t, e, f = _curve()
    s = analyze(t, e, f, break_drop_pct=40, break_min_n=5, stop_reason="BREAK")
    assert s.peak_load_n == pytest.approx(400, rel=1e-3)
    assert s.extension_at_peak_mm == pytest.approx(1.2, abs=0.01)
    assert s.break_detected
    assert s.break_extension_mm == pytest.approx(1.6, abs=0.01)
    assert s.stiffness_n_per_mm == pytest.approx(400, rel=1e-6)
    assert s.stiffness_r2 == pytest.approx(1.0)
    assert s.energy_to_peak_j > 0 and s.energy_to_end_j > s.energy_to_peak_j
    assert s.uts_mpa is None and s.modulus_mpa is None
    assert "BREAK" in s.to_text()


def test_geometry_gives_stress_values():
    t, e, f = _curve()
    s = analyze(t, e, f, area_mm2=10.0, gauge_length_mm=50.0, break_drop_pct=40, break_min_n=5)
    assert s.uts_mpa == pytest.approx(40.0, rel=1e-3)
    assert s.modulus_mpa == pytest.approx(400 * 50 / 10, rel=1e-6)
    assert s.elongation_at_break_pct == pytest.approx(100 * 1.6 / 50, abs=0.05)
    d = s.to_dict()
    assert d["uts_mpa"] == pytest.approx(40.0, rel=1e-3)


def test_no_break_when_load_never_drops():
    assert find_break(np.linspace(0, 100, 50), 40, 5) is None


def test_break_needs_minimum_peak():
    noise = np.array([0.0, 2.0, 0.5, 0.1])
    assert find_break(noise, 40, 5) is None
    assert find_break(noise, 40, 1) == 1


def test_handles_nan_and_empty():
    s = analyze([0, 1], [0, 1], [float("nan"), float("nan")])
    assert s.n_samples == 2 and not s.break_detected
    s = analyze([], [], [])
    assert s.n_samples == 0
