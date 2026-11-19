"""Screenshots of the real GUI for the manual, taken offscreen against the simulator.

Builds MainWindow the way `vsbc gui --sim` does (styled, light theme, 1280 x 800), connects to the
simulated Mega and camera, runs one short test to break, and saves PNGs to docs/manual/img/.
It works in a temporary output folder, so your own Outputs/ and preferences are not touched.

Needs the app's GUI dependencies (PyQt5, pyqtgraph, pyserial, numpy, matplotlib); see build_manual.py.
Run it on its own with:  python docs/manual/capture_screenshots.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
IMG = HERE / "img"

os.environ["QT_QPA_PLATFORM"] = "offscreen"
if os.name == "nt":
    # The offscreen platform finds no fonts on Windows by itself (every label would be blank).
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
# Render at 1.5x so the screenshots stay sharp in print; the layout is still that of a 1280 x 800 screen.
os.environ.setdefault("QT_SCALE_FACTOR", "1.5")
sys.path.insert(0, str(REPO))

from vsbc import sim as sim_module  # noqa: E402
from vsbc.camera.roi import Roi, rect_mask  # noqa: E402
from vsbc.config import Config  # noqa: E402
from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import QtWidgets, apply_style  # noqa: E402
from vsbc.gui.wizards import LoadCalibrationDialog  # noqa: E402
from vsbc.sim import SimulatedMega, Specimen  # noqa: E402

# The specimen from tests/conftest.py: breaks at 1.6 mm, so the test is over in a couple of seconds.
FAST_SPECIMEN = dict(stiffness_n_per_mm=400.0, yield_load_n=300.0, peak_load_n=400.0,
                     extension_at_peak_mm=1.2, break_extension_mm=1.6)
SIZE = (1280, 800)


def _wait(app, predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _settle(app, seconds=0.4) -> None:
    """Let timers (readouts, plots, the readiness tick) catch up before a grab."""
    _wait(app, lambda: False, seconds)


def capture(out_dir: Path = IMG) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    saved: list[Path] = []
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("VSBC Tensile Tester")
    sim_module.AVERAGE_S = 0.25                     # TARE / CAL average for 2 s on the Mega; less is fine here

    tmp = Path(tempfile.mkdtemp(prefix="vsbc_manual_"))
    cfg = Config(output_dir=tmp / "Outputs")
    cfg.serial.ready_timeout_s = 2.0
    cfg.ping_interval_s = 0.2
    cfg.loadcell.rate_hz = 80.0                     # the simulator's rate
    cfg.camera.auto_start = False
    cfg.gui.touch_scroll = False
    apply_style(app, cfg.gui.font_pt, cfg.gui.scale, size=SIZE, theme_name="light")

    def shot(name: str, widget=None) -> None:
        _settle(app)
        path = out_dir / f"{name}.png"
        if not (widget or win).grab().save(str(path)):
            raise RuntimeError(f"could not save {path}")
        saved.append(path)
        print(f"  {path.name}")

    win = MainWindow(cfg, sim=True, interactive=False)
    win.resize(*SIZE)
    # The offscreen "screen" is smaller than 1280 x 800, which caps the right side's width: give it
    # the width it gets on a real 1280 x 800 screen.
    win.tabs.setMinimumWidth(win._tabs_min_width())
    win.show()
    try:
        # 1. Connect to the simulated Mega and start the simulated camera.
        win.tabs.setCurrentIndex(0)
        win.connect_machine(None, sim=SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=9))
        if not _wait(app, lambda: win.machine.connected and win.machine.latest is not None
                     and win.connect_btn.text() == "Disconnect", 15):
            raise RuntimeError("the simulator did not connect")
        camera_ok = win.camera is not None
        if camera_ok:
            win.camera.start()
            camera_ok = _wait(app, lambda: win.camera.state == "preview" and win.camera.preview is not None, 30) \
                and _wait(app, lambda: win.camera_view.image.image is not None, 5)
            if not camera_ok:
                print("  (the simulated camera did not start: skipping the camera screenshots)")
        if camera_ok:
            # An ROI over the middle of the frame, with the notch tip near the left edge.
            info = win.camera.info
            h, w = info["height"], info["width"]
            win.set_roi(Roi(rect_mask((h, w), 0.12 * w, 0.18 * h, 0.88 * w, 0.82 * h),
                            seed=(w // 2, h // 2), notch=(int(0.14 * w), h // 2)), None)
        win.refresh_readiness()
        _wait(app, lambda: [i.key for i in win.readiness_items if i.required] == ["specimen"], 5)
        win.test_panel.specimen.setText("")
        _wait(app, lambda: not win.test_panel.start_btn.isEnabled(), 2)
        shot("02_checklist")

        # 2. The Camera tab (right) and what records with the test.
        if camera_ok:
            win.left_tabs.setCurrentWidget(win.camera_view)
            win.tabs.setCurrentIndex(win._find_tab(win.tabs, "Camera"))
            shot("03_camera_tab", win.tabs)
            panel = win.camera_panel
            panel.settle.setValue(0.2)
            # No pyvale or OpenCV in this environment: only the sync (the crack frames need nothing else).
            for box in (panel.run_dic, panel.run_strain, panel.crack_image, panel.crack_opening, panel.make_video):
                box.setChecked(False)

        # 3. A test to break.
        win.tabs.setCurrentIndex(0)
        test = win.test_panel
        test.specimen.setText("DEMO-1")
        test.operator.setText("Operator")
        test.rate.set_si_value(60.0)
        test.max_ext.set_si_value(5.0)
        test.area.set_si_value(10.0)
        test.gauge.set_si_value(50.0)
        _wait(app, test.start_btn.isEnabled, 3)
        win.start_test()
        if win.runner is None:
            raise RuntimeError("the test did not start")
        if not _wait(app, lambda: win.runner is None and win.last_result is not None, 60):
            raise RuntimeError("the test did not finish")
        folder = win.last_result.folder
        if camera_ok:
            _wait(app, lambda: (folder / "camera" / "sync.json").is_file(), 30)
        # Post-processing and the upload zip: wait until the status bar has nothing running for a while.
        quiet_since = [None]

        def quiet() -> bool:
            if win.postprocess.busy or not win.post_status.isHidden():
                quiet_since[0] = None
                return False
            quiet_since[0] = quiet_since[0] or time.monotonic()
            return time.monotonic() - quiet_since[0] > 2.0
        _wait(app, quiet, 90)
        win.tabs.setCurrentIndex(0)
        win.left_tabs.setCurrentWidget(win.plots)
        win.plots.setCurrentIndex(0)
        _settle(app)
        win.tabs.widget(0).ensureWidgetVisible(test.view_btn, 0, 0)
        shot("04_test_result")

        # 4. Results > Overview, on the crack frames.
        win.show_results(folder)
        view = win.results_view
        _wait(app, lambda: view.res is not None and view.res.folder == folder, 15)
        if camera_ok and view.res is not None and view.res.crack is not None:
            view.jump_to_crack()
        shot("06_results_overview")

        # 5. Results > DIC analysis > Run: the checklist for this test.
        win.show_dic()
        win.dic_view.inner.setCurrentWidget(win.dic_view.run_pane)
        _settle(app, 1.5)
        shot("07_dic_run")

        # 6. The other tabs on the right.
        win.left_tabs.setCurrentWidget(win.plots)
        win.tabs.setCurrentIndex(win._find_tab(win.tabs, "Machine"))
        win.machine_panel.calibration_section.set_expanded(True)
        _settle(app)
        shot("08_calibration", win.machine_panel.calibration_section)   # all of it, not only what fits on screen
        win.tabs.setCurrentIndex(win.outputs_index)
        win.outputs_panel.refresh()
        _settle(app, 1.0)
        shot("09_outputs", win.tabs)
        win.tabs.setCurrentIndex(win._find_tab(win.tabs, "System"))
        for i, name in enumerate(("10_display", "11_console", "12_firmware")):
            win.system_panel.tabs.setCurrentIndex(i)
            shot(name, win.tabs)

        # 7. The load-cell calibration wizard, on its own.
        dialog = LoadCalibrationDialog(win)
        dialog.resize(720, 600)
        dialog.show()
        shot("13_loadcal_wizard", dialog)
        dialog.reject()
    finally:
        if win.camera is not None:
            try:
                win.camera.stop()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        win.close()
        app.processEvents()
        shutil.rmtree(tmp, ignore_errors=True)
    return saved


if __name__ == "__main__":
    print(f"Screenshots -> {IMG}")
    print(f"{len(capture())} screenshots saved.")
