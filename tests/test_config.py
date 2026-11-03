import pytest

from vsbc.config import REPO_ROOT, Config, load_config


def test_example_config_matches_the_defaults():
    cfg = load_config(REPO_ROOT / "config.example.toml")
    defaults = Config()
    assert cfg.serial == defaults.serial
    assert cfg.loadcell == defaults.loadcell
    assert cfg.test == defaults.test
    assert cfg.gui == defaults.gui
    assert cfg.camera == defaults.camera
    assert cfg.units == defaults.units
    assert cfg.storage == defaults.storage
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


def test_bad_choices_are_rejected(tmp_path):
    path = tmp_path / "c.toml"
    for text, key in (('[units]\nforce = "kgf"\n', "units.force"), ('[units]\nlength = "ft"\n', "units.length"),
                      ('[camera]\nsave_mode = "some"\n', "camera.save_mode"), ("[loadcell]\nrate_hz = 0\n", "loadcell.rate_hz")):
        path.write_text(text)
        with pytest.raises(ValueError, match=key):
            load_config(path)


def test_camera_dir_is_relative_to_the_repository(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[camera]\ndir = "my_camera"\n')
    assert load_config(path).camera_dir == REPO_ROOT / "my_camera"


def test_missing_explicit_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")
