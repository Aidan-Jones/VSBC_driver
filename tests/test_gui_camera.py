"""The GUI's camera, units, outputs and load-sign features, offscreen against the simulators."""

import csv
import os
import time

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vsbc.camera.roi import Roi, rect_mask  # noqa: E402
from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import QtWidgets  # noqa: E402
from vsbc.sim import SimulatedMega, Specimen  # noqa: E402
from vsbc.units import Units  # noqa: E402
from tests.conftest import FAST_SPECIMEN  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _wait(app, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def window(app, config):
    win = MainWindow(config, interactive=False)
    win.resize(1280, 800)
    win.show()
    sim = SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=5)
    win.connect_machine(None, sim=sim)
    assert _wait(app, lambda: win.machine.connected and win.machine.latest is not None
                 and win.connect_btn.text() == "Disconnect")
    win.sim = sim
    yield win
    win.close()
    app.processEvents()


def test_units_change_the_display_not_the_values(app, window):
    win = window
    panel = win.test_panel
    rate_mm_min = panel.rate.si_value()
    win.set_units(Units("lbf", "in"))
    assert _wait(app, lambda: win.load_label.text().endswith("lbf") and win.pos_label.text().endswith("in"), 2)
    assert panel.rate.suffix().strip() == "in/min"
    assert panel.rate.si_value() == pytest.approx(rate_mm_min)
    panel.max_ext.setValue(0.1)                                   # typed in inches
    assert panel.max_ext.si_value() == pytest.approx(2.54)
    assert win.jog_panel.step_group.button(0).text() == "0.001 in"
    assert win.plots.test_plot.getPlotItem().getAxis("left").labelText == "Load (lbf)"
    win.units_panel.reset()
    assert win.units == Units() and panel.rate.suffix().strip() == "mm/min"
    assert panel.max_ext.si_value() == pytest.approx(2.54)


def test_flip_load_sign(app, window):
    win = window
    scale = win.machine.settings["load_scale"]
    win.calibrate_panel.flip_load_sign()
    assert _wait(app, lambda: win.machine.settings.get("load_scale") == pytest.approx(-scale), 5)
    assert "counts/N" in win.calibrate_panel.values["load_scale"].text()
    win.calibrate_panel.flip_load_sign()
    assert _wait(app, lambda: win.machine.settings.get("load_scale") == pytest.approx(scale), 5)


def test_reversed_tension_is_flagged_during_the_test(app, window):
    win = window
    win.machine.set("load_scale", -win.machine.settings["load_scale"])
    win.sim.remount_specimen()
    win.test_panel.specimen.setText("NEG")
    win.test_panel.rate.set_si_value(300.0)
    win.test_panel.max_ext.set_si_value(1.0)
    win.test_panel.break_drop.setValue(0.0)
    win.start_test()
    assert _wait(app, lambda: win.runner is None and win.last_result is not None, 15)
    assert any("Tension is reading negative" in w for w in win._warnings)
    assert any("sign is reversed" in w for w in win.last_result.warnings)


def test_load_calibration_with_weights_on_top_reads_tension_positive(app, window):
    from vsbc.gui.wizards import LoadCalibrationDialog

    win = window
    dialog = LoadCalibrationDialog(win)
    dialog.direction.setCurrentIndex(1)                           # compression: weights placed on it
    dialog.tare_and_zero()
    assert _wait(app, lambda: not dialog._busy and dialog.points, 15)
    dialog.value.setValue(5.0)
    dialog.unit.setCurrentText("kg")
    dialog.record()
    assert _wait(app, lambda: not dialog._busy and dialog.fit is not None, 15)
    assert dialog.points[-1].force_n < 0
    assert dialog.fit.scale == pytest.approx(win.sim.true_scale, rel=0.02)   # tension positive
    dialog.done(0)


def test_outputs_tab_lists_the_test(app, window):
    win = window
    win.test_panel.specimen.setText("OUT-1")
    win.test_panel.rate.set_si_value(300.0)
    win.test_panel.max_ext.set_si_value(5.0)
    win.start_test()
    assert _wait(app, lambda: win.runner is None and win.last_result is not None, 15)
    win.outputs_panel.refresh()
    assert _wait(app, lambda: any(e.name == "OUT-1" for e in win.outputs_panel.entries), 10)
    assert win.outputs_panel.table.rowCount() == len(win.outputs_panel.entries)
    assert "used" in win.outputs_panel.usage.format()
    win.config.storage.warn_free_gb = 1e9                         # pretend the disk is nearly full
    win.check_storage()
    assert any(w.startswith("Storage low") for w in win._warnings)
    assert win.tabs.tabText(win.outputs_index) == "Outputs ⚠"


def test_test_recorded_with_the_simulated_camera(app, config):
    win = MainWindow(config, sim=True, interactive=False)
    win.resize(1280, 800)
    win.show()
    try:
        win.connect_machine(None, sim=SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=9))
        win.camera.start()
        assert _wait(app, lambda: win.machine.connected and win.camera.state == "preview"
                     and win.camera.preview is not None, 30)
        assert _wait(app, lambda: win.camera_view.image.image is not None, 5)
        info = win.camera.info
        win.set_roi(Roi(rect_mask((info["height"], info["width"]), 20, 20, info["width"] - 20, info["height"] - 20),
                        seed=(info["width"] // 2, info["height"] // 2), notch=(25, info["height"] // 2)), None)
        panel = win.camera_panel
        panel.settle.setValue(0.2)
        for box in (panel.run_dic, panel.run_strain, panel.crack_image, panel.crack_opening):
            box.setChecked(False)                                 # no pyvale here: only the video
        win.test_panel.specimen.setText("CAM-GUI")
        win.test_panel.rate.set_si_value(60.0)
        win.test_panel.max_ext.set_si_value(5.0)
        win.start_test()
        assert win.runner is not None
        assert _wait(app, lambda: win.runner is None and win.last_result is not None, 30)
        folder = win.last_result.folder
        assert win.last_result.stop_reason == "BREAK"
        assert _wait(app, lambda: (folder / "camera" / "sync.json").is_file(), 30)
        rows = list(csv.DictReader(open(folder / "data.csv", encoding="utf-8")))
        assert rows and all(r["frame"] for r in rows)
        assert _wait(app, lambda: (folder / "video.mp4").is_file() and not win.postprocess.busy, 60)
        assert win.postprocess.done and win.postprocess.done[-1].ok, win.postprocess.summary()
        assert "Camera:" in win.test_panel.result.toPlainText()

        # Results tab: the simulated specimen cracks at the break, and the RAM buffer was saved then.
        win.test_panel.view_btn.click()
        view = win.results_view
        assert win.left_tabs.currentWidget() is win.results_tab and win.results_tab.inner.currentWidget() is view
        assert _wait(app, lambda: view.res is not None and view.res.test == folder, 10)
        crack = view.res.crack
        assert crack is not None and crack.source == "break" and crack.n_frames >= 1
        assert view.res.video is not None and view.video_btn.isEnabled()
        view.jump_to_crack()
        assert crack.start <= view.pos <= crack.end
    finally:
        win.close()
        app.processEvents()


def test_manual_recording_blocks_a_test_and_holds_the_exposure(app, config):
    win = MainWindow(config, sim=True, interactive=False)
    win.resize(1280, 800)
    win.show()
    try:
        win.connect_machine(None, sim=SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=11))
        win.camera.start()
        assert _wait(app, lambda: win.machine.connected and win.camera.state == "preview", 30)
        view = win.camera_view
        assert _wait(app, lambda: view.exp_up.isEnabled() and view.exp_down.isEnabled())
        win.camera_panel.settle.setValue(0.0)
        win.start_manual_recording("manual")
        assert _wait(app, lambda: win.camera.state == "recording", 15)
        # DIC compares every frame with frame 0: no exposure changes while recording.
        assert _wait(app, lambda: not view.exp_up.isEnabled() and not view.exp_down.isEnabled())
        errors, _warnings, record = win.camera_panel.preflight()
        assert not record and any("camera recording is still running" in e for e in errors)
        win.stop_camera_recording()
        assert _wait(app, lambda: win.camera.state == "preview" and win.camera.recording is None, 30)
        assert _wait(app, lambda: view.exp_up.isEnabled())
        assert not win.camera_panel.preflight()[0]
    finally:
        win.close()
        app.processEvents()
