import pytest

from vsbc import sim as sim_module
from vsbc.config import Config
from vsbc.machine import Machine
from vsbc.sim import SimulatedMega, Specimen

# Breaks at 1.6 mm, so a test at 5 mm/s is over in about a third of a second.
FAST_SPECIMEN = dict(stiffness_n_per_mm=400.0, yield_load_n=300.0, peak_load_n=400.0,
                     extension_at_peak_mm=1.2, break_extension_mm=1.6)


@pytest.fixture(autouse=True)
def quick_averaging(monkeypatch):
    """The firmware averages TARE / CAL for 2 s; the simulated tests do with less."""
    monkeypatch.setattr(sim_module, "AVERAGE_S", 0.25)


@pytest.fixture
def config(tmp_path):
    cfg = Config(output_dir=tmp_path / "out")
    cfg.serial.ready_timeout_s = 2.0
    cfg.ping_interval_s = 0.2
    cfg.camera.auto_start = False     # tests that need the camera start the simulated one themselves
    cfg.loadcell.rate_hz = 80.0       # the simulator's rate, which keeps the timing tests short
    return cfg


@pytest.fixture
def sim():
    s = SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=1)
    yield s
    s.close()


@pytest.fixture
def machine(config, sim):
    m = Machine(config)
    m.connect(sim=sim)
    yield m
    m.disconnect()
