import pytest

from vsbc.config import Config
from vsbc.machine import Machine
from vsbc.sim import SimulatedMega, Specimen

# Breaks at 1.6 mm, so a test at 5 mm/s is over in about a third of a second.
FAST_SPECIMEN = dict(stiffness_n_per_mm=400.0, yield_load_n=300.0, peak_load_n=400.0,
                     extension_at_peak_mm=1.2, break_extension_mm=1.6)


@pytest.fixture
def config(tmp_path):
    cfg = Config(output_dir=tmp_path / "out")
    cfg.serial.ready_timeout_s = 2.0
    cfg.ping_interval_s = 0.2
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
