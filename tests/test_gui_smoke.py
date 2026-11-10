"""Drives the real main window (offscreen) against the simulator."""

import os
import time

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import QtWidgets  # noqa: E402
from vsbc.sim import SimulatedMega, Specimen  # noqa: E402
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


def test_gui_connect_jog_and_pull_test(app, config, tmp_path):
    win = MainWindow(config, interactive=False)
    win.resize(1280, 800)
    win.show()
    try:
        # The Camera tab comes first and is the one showing.
        assert win.plots.tabText(0) == "Camera" and win.plots.currentWidget() is win.camera_view
        assert [win.plots.tabText(i) for i in (1, 2)] == ["Load – extension", "Load && position – time"]
        win.connect_machine(None, sim=SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=3))
        assert _wait(app, lambda: win.machine.connected and win.machine.latest is not None
                     and win.connect_btn.text() == "Disconnect")
        assert win.state_label.text() == "IDLE"
        assert _wait(app, lambda: "background" in win.state_label.styleSheet(), 2)     # the state pill
        assert win.test_panel.start_btn.isEnabled()

        # Jog +1 mm (default step) at the jog rate.
        win.jog_panel.jog(+1)
        assert _wait(app, lambda: abs(win.machine.latest.pos_mm - 1.0) < 1e-6 and not win.machine.latest.moving, 5)
        # The readouts refresh on the GUI timer, a moment after the sample arrives.
        assert _wait(app, lambda: win.pos_label.text().startswith("1.0000"), 2)

        # Pull test to break.
        win.test_panel.specimen.setText("GUI-1")
        win.test_panel.rate.setValue(300.0)
        win.test_panel.max_ext.setValue(5.0)
        win.test_panel.area.setValue(10.0)
        win.test_panel.gauge.setValue(50.0)
        win.start_test()
        assert win.runner is not None
        assert _wait(app, lambda: win.runner is None and win.last_result is not None, 15)
        result = win.last_result
        assert result.stop_reason == "BREAK" and not result.aborted
        assert (result.folder / "data.csv").is_file()
        assert "Peak load" in win.test_panel.result.toPlainText()
        assert "Specimen: GUI-1" in win.test_panel.result.toPlainText()
        assert win.test_panel.specimen.text() == "GUI-2"                 # counts up for the next specimen
        assert "break" in win.test_panel.stop_card.value.text().lower()
        assert len(win.plots._ext) > 10
        assert win.plots.currentWidget() is win.camera_view       # a test does not switch away from the camera

        # STOP during a jog.
        win.jog_panel.rate.setValue(1.0)
        win.jog_panel.step_group.button(3).setChecked(True)   # 10 mm
        win.jog_panel.jog(+1)
        assert _wait(app, lambda: win.machine.latest.moving, 3)
        win.stop_all()
        assert _wait(app, lambda: not win.machine.latest.moving, 3)
        assert _wait(app, lambda: "Move ended (STOP)" in win.console.text.toPlainText(), 2)

        shot = tmp_path / "gui.png"
        assert win.grab().save(str(shot))
        assert shot.stat().st_size > 10_000
    finally:
        win.close()
        app.processEvents()


def test_gui_remembers_layout_theme_and_test_settings(app, config):
    from vsbc.gui import common

    win = MainWindow(config, interactive=False)
    win.show()
    try:
        win.test_panel.operator.setText("Sam")
        win.test_panel.specimen.setText("B07")
        win.test_panel.rate.setValue(12.0)
        win.tabs.setCurrentWidget(win.jog_panel)
        win.set_theme("dark")
        assert common.theme() is common.DARK

        # Banner: the × hides ordinary warnings; critical ones stay until their cause is gone.
        win._add_warning("Load cell not calibrated.")
        win._add_warning("Connection lost: unplugged")
        win.dismiss_warnings()
        assert not win.banner.isHidden() and win.banner_close.isHidden()
        assert "Connection lost" in win.banner_text.text() and "not calibrated" not in win.banner_text.text()
        win._remove_warnings("Connection lost")
        assert win.banner.isHidden()
        win._add_warning("Load cell not calibrated.")      # still dismissed: it never went away
        assert win.banner.isHidden()
    finally:
        win.close()
        app.processEvents()
    again = MainWindow(config, interactive=False)
    try:
        panel = again.test_panel
        assert panel.operator.text() == "Sam" and panel.specimen.text() == "B07"
        assert panel.rate.si_value() == pytest.approx(12.0)
        assert again.tabs.currentWidget().widget() is again.jog_panel
        prefs = again._gui_prefs()
        assert prefs["theme"] == "dark" and prefs["geometry"] and len(prefs["splitter"]) == 2
    finally:
        common.set_theme(QtWidgets.QApplication.instance(), "light")
        again.close()
        app.processEvents()
