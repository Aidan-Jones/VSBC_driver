"""The Ready checklist: what is left before a test, and the Fix buttons that open the place to do it."""

import os
import time
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import QtWidgets  # noqa: E402
from vsbc.gui.readiness import first_todo, readiness  # noqa: E402
from vsbc.machine import Status  # noqa: E402
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


def _status(**changes):
    values = dict(state="IDLE", pos_mm=0.0, load_n=0.0, raw=0, rate_mm_s=1.0, motors="AB", referenced=True,
                  loadcell_ok=True, calibrated=True, sample_rate_hz=80.0, streaming=True, dropped_lines=0)
    values.update(changes)
    return Status(**values)


def _window(connected=True, status=None, specimen="A1", warnings=(), saved=False):
    """Just what readiness() reads."""
    return NS(machine=NS(connected=connected, last_status=status), reveal=lambda _w: None, toggle_connection=None,
              calibrate_panel=NS(load_btn=None, flip_btn=None), jog_panel=NS(reference_box=None, motors=None),
              has_saved_position=saved, _warnings=list(warnings), test_panel=NS(specimen=NS(text=lambda: specimen)),
              camera=None)


def _todo(win):
    return [i.key for i in readiness(win) if i.required]


def test_readiness_lists_what_is_left_in_order():
    assert _todo(_window(connected=False, specimen="")) == ["connect", "specimen"]
    assert _todo(_window(status=None)) == ["status"]                       # connected, STATUS not read yet
    assert _todo(_window(status=_status())) == []
    assert first_todo(readiness(_window(status=_status()))) is None
    win = _window(status=_status(calibrated=False, referenced=False, motors="A"), specimen="", saved=True)
    assert _todo(win) == ["loadcell", "reference", "motors", "specimen"]
    items = {i.key: i for i in readiness(win)}
    assert "restore" in items["reference"].text and items["motors"].text.endswith("A only)")
    assert _todo(_window(status=_status(loadcell_ok=False))) == ["loadcell"]
    # A reversed load sign is worth a look, but does not block the test.
    signs = readiness(_window(status=_status(), warnings=["Tension is reading negative: ..."]))
    assert [(i.key, i.level) for i in signs if i.level != "ok"] == [("sign", "warn")]


def test_fix_opens_the_folded_section_it_points_to(app, config):
    win = MainWindow(config, interactive=False)
    win.resize(1280, 800)
    win.show()
    try:
        win.connect_machine(None, sim=SimulatedMega(specimen=Specimen(**FAST_SPECIMEN), seed=4))
        assert _wait(app, lambda: win.machine.connected and win.connect_btn.text() == "Disconnect")
        win.test_panel.specimen.setText("R-1")
        win.machine.select_motors("A")            # service mode: a test cannot start
        assert _wait(app, lambda: "motors" in [i.key for i in win.readiness_items if i.required], 5)
        assert _wait(app, lambda: not win.test_panel.start_btn.isEnabled(), 2)
        assert "both motors" in win.test_panel.start_hint.text()
        section = win.machine_panel.service_section
        assert not section.is_expanded()
        win.tabs.setCurrentWidget(win.test_panel)
        win.test_panel.ready.fix_buttons["motors"].click()
        assert _wait(app, lambda: section.is_expanded() and win.tabs.currentWidget().isAncestorOf(section), 2)
        win.machine.select_motors("AB")
        assert _wait(app, lambda: win.test_panel.start_btn.isEnabled(), 5)
        assert "Ready" in win.ready_chip.text()
    finally:
        win.close()
        app.processEvents()


def test_connect_button_in_the_checklist_connects(app, config):
    win = MainWindow(config, interactive=False)
    win.resize(1280, 800)
    win.show()
    try:
        win.select_port("SIM")                      # the port chosen in the top bar
        win.refresh_readiness()
        item = next(i for i in win.readiness_items if i.key == "connect")
        assert item.required and "Simulator" in item.text
        win.test_panel.ready.fix_buttons["connect"].click()
        assert win._connecting and "connect" not in win.test_panel.ready.fix_buttons    # no second press
        assert _wait(app, lambda: win.machine.connected and win.connect_btn.text() == "Disconnect")
        assert _wait(app, lambda: "connect" not in [i.key for i in win.readiness_items if i.required], 3)
    finally:
        win.close()
        app.processEvents()
