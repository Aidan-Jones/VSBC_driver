import ast

import numpy as np
import pytest

from vsbc.camera import tiff
from vsbc.camera.jobs import dic_selection, run_script
from vsbc.camera.roi import Roi, load_roi, outline, polygon_mask, rect_mask, save_roi
from vsbc.camera.scripts import SCRIPTS, check_script, check_scripts, literal_settings, override_settings
from vsbc.config import REPO_ROOT

CAMERA_DIR = REPO_ROOT / "camera"


def test_shipped_scripts_meet_the_interface():
    checks = check_scripts(CAMERA_DIR)
    assert [c.name for c in checks] == list(SCRIPTS)
    for check in checks:
        assert check.ok, check.text()


def test_missing_settings_and_unguarded_main_are_reported(tmp_path):
    (tmp_path / "crack_tip_image.py").write_text("RUN_FOLDER = 'x'\nCRACK_FROM_LEFT = True\n")
    assert "NOTCH_TIP" in check_script(tmp_path / "crack_tip_image.py").text()
    (tmp_path / "capture_dic_live.py").write_text("SAVE_EVERY = 3\ndef main():\n    pass\nmain()\n")
    check = check_script(tmp_path / "capture_dic_live.py")
    assert not check.ok and "dic_worker" in check.text() and "on import" in check.text()
    assert not check_script(tmp_path / "pyvale_dic_run.py").found


def test_settings_are_read_without_importing():
    s = literal_settings(CAMERA_DIR)
    assert s["SAVE_EVERY"] == 9 and s["DIC_STEP"] == 27 and s["subset_size"] == 41
    assert s["CROP"] == {"width": 1920, "height": 400, "offset_x": 8, "offset_y": 408}
    assert s["DIC_THREADS"] == 2          # an expression in the script: the built-in value


def test_override_inserts_after_the_setting():
    source = "import os\nA = b = 1  # both\nRUN_FOLDER = r'C:\\x'\nNOTCH = None\n\ndef main():\n    return RUN_FOLDER\n"
    out = override_settings(source, {"RUN_FOLDER": "/home/pi/test", "NOTCH": [5, 6]})
    namespace = {}
    exec(compile(out, "<test>", "exec"), namespace)
    assert namespace["main"]() == "/home/pi/test" and namespace["NOTCH"] == [5, 6] and namespace["b"] == 1
    ast.parse(out)
    with pytest.raises(KeyError):
        override_settings(source, {"MISSING": 1})


@pytest.mark.parametrize("name, overrides", [
    ("pyvale_dic_run.py", {"image_folder": "/run/images", "output_folder": "/run/pyvale_results", "RUN_DIC": False,
                           "MAKE_ANIMATION": True, "ANIM_STEP": 3}),
    ("crack_tip_image.py", {"RUN_FOLDER": "/run", "NOTCH_TIP": [25, 80], "CRACK_FROM_LEFT": False}),
    ("crack_tip_opening.py", {"RUN_FOLDER": "/run", "NOTCH_TIP": [25, 80], "CRACK_FROM_LEFT": True,
                              "subset_size": 31}),
])
def test_shipped_analysis_scripts_take_the_test_settings(name, overrides):
    source = (CAMERA_DIR / name).read_text(encoding="utf-8")
    tree = ast.parse(override_settings(source, overrides))
    seen = {}
    for node in tree.body:                       # the last top-level assignment of each name wins
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in overrides:
            seen[node.targets[0].id] = ast.literal_eval(node.value)
    assert seen == overrides


def test_run_script_saves_figures_and_uses_the_test_folder(tmp_path):
    script = tmp_path / "plotter.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "RUN_FOLDER = 'somewhere else'\n"
        "def main():\n"
        "    open(RUN_FOLDER + '/out.txt', 'w').write('ran')\n"
        "    plt.plot([0, 1], [0, 1])\n"
        "    plt.show()\n"
        "    plt.plot([0, 1], [1, 0])\n"
        "    plt.show()\n"
        "if __name__ == '__main__':\n"
        "    main()\n")
    figures = run_script(script, {"RUN_FOLDER": str(tmp_path)}, tmp_path, tmp_path)
    assert (tmp_path / "out.txt").read_text() == "ran"
    assert [f.name for f in figures] == ["plotter_01.png", "plotter_02.png"]
    assert all(f.stat().st_size > 1000 for f in figures)


def test_dic_selection():
    saved = np.array([0, 6, 12, 18, 24, 30, 100, 101, 102, 103, 104])
    overview, window = dic_selection(saved, [[100, 104]], overview_every=2, crack_every=2)
    assert overview == [6, 18, 30] and window == [100, 102, 104]


def test_roi_round_trip_and_masks(tmp_path):
    mask = rect_mask((40, 100), 10, 5, 90, 35)
    assert mask.sum() == 80 * 30
    tri = polygon_mask((40, 100), [(10, 5), (90, 5), (10, 35)])
    assert 0 < tri.sum() < mask.sum() and tri[6, 11] and not tri[34, 89]
    roi = Roi(mask, seed=(50, 20), notch=(12, 20), crack_from_left=False, offsets=(8, 408))
    assert roi.problems() == [] and roi.matches(40, 100, (8, 408)) and not roi.matches(40, 100, (0, 0))
    save_roi(tmp_path / "roi.npz", roi)
    back = load_roi(tmp_path / "roi.npz")
    assert np.array_equal(back.mask, mask) and back.seed == (50, 20) and back.notch == (12, 20)
    assert back.crack_from_left is False and back.offsets == (8, 408)
    assert Roi(mask, seed=(0, 0)).problems() == ["the seed point is outside the ROI"]
    xs, ys = outline(mask)
    assert xs.min() == 10 and xs.max() == 89 and ys.min() == 5 and ys.max() == 34
    assert load_roi(tmp_path / "missing.npz") is None


def test_tiff_round_trip(tmp_path):
    image = (np.arange(160 * 64) % 251).astype(np.uint8).reshape(64, 160)
    tiff.write_gray8(tmp_path / "f.tif", image)
    assert np.array_equal(tiff.read(tmp_path / "f.tif"), image)
    cv2 = pytest.importorskip("cv2")
    assert np.array_equal(cv2.imread(str(tmp_path / "f.tif"), cv2.IMREAD_GRAYSCALE), image)
