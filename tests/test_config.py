import pytest

from vsbc.config import REPO_ROOT, Config, load_config


def test_example_config_matches_the_defaults():
    cfg = load_config(REPO_ROOT / "config.example.toml")
    defaults = Config()
    assert cfg.serial == defaults.serial
    assert cfg.loadcell == defaults.loadcell
    assert cfg.test == defaults.test
    assert cfg.gui == defaults.gui
    assert cfg.watchdog_ms == defaults.watchdog_ms
    assert cfg.output_dir == REPO_ROOT / "Outputs"


def test_overrides_and_relative_output_dir(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('output_dir = "data"\n[loadcell]\ncapacity_n = 2000\n[serial]\nport = "COM7"\n')
    cfg = load_config(path)
    assert cfg.loadcell.capacity_n == 2000
    assert cfg.serial.port == "COM7"
    assert cfg.output_dir == tmp_path / "data"
    assert cfg.tests_dir == tmp_path / "data" / "tests"


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[loadcell]\ncapcity_n = 5\n")
    with pytest.raises(ValueError, match="loadcell.capcity_n"):
        load_config(path)


def test_missing_explicit_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")
