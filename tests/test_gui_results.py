"""The Results tab, FlowTabs.setTabEnabled and the idle time window, offscreen."""

import os
import time

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import FlowTabs, QtWidgets  # noqa: E402
from vsbc.gui.plots import IDLE_WINDOW_S, LivePlots  # noqa: E402
from vsbc.protocol import Sample  # noqa: E402
from tests.test_results import make_test  # noqa: E402


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


class _Runner:
    """Stands in for a running PullTestRunner."""
    recorder = None

    def request_stop(self, *_args):
        pass


def test_flow_tabs_disabled_tab_is_left(app):
    tabs = FlowTabs()
    for name in ("a", "b", "c"):
        tabs.addTab(QtWidgets.QLabel(name), name)
    tabs.setCurrentIndex(2)
    tabs.setTabEnabled(2, False)
    assert tabs.currentIndex() == 0 and not tabs.isTabEnabled(2)
    tabs.setCurrentIndex(2)                                       # a disabled tab cannot be chosen
    assert tabs.currentIndex() == 0
    tabs.setTabEnabled(2, True)
    tabs.setCurrentIndex(2)
    assert tabs.currentIndex() == 2


def _sample(t_s: float) -> Sample:
    return Sample(t_ms=t_s * 1000.0, pos_mm=0.0, load_n=1.0, raw=0, state="I")


def test_time_plot_shows_the_last_20_s_until_the_first_test(app):
    plots = LivePlots(60.0)
    for k in range(600):                                          # 60 s at 10 samples/s
        plots.add_sample(_sample(k / 10.0))
    plots.refresh()
    t, _ = plots.load_curve.getData()
    assert t[-1] - t[0] <= IDLE_WINDOW_S + 1e-9
    plots.begin_test()
    plots.add_sample(_sample(60.0))
    plots.refresh()
    t, _ = plots.load_curve.getData()
    assert t[-1] - t[0] == pytest.approx(60.0)                    # after a test: everything kept


def test_results_tab_shows_the_crack_frames(app, config):
    folder = make_test(config.output_dir / "tests" / "2026-11-05", name="120000_T1", jump_frame=305)
    win = MainWindow(config, interactive=False)
    win.resize(1280, 800)
    win.show()
    view = win.results_view
    try:
        assert win.plots.tabText(win.results_index) == "Results"
        win.show_results(folder)
        assert win.plots.currentWidget() is view
        assert _wait(app, lambda: view.res is not None and view.res.crack is not None
                     and view.res.crack.estimate is not None, 10)
        res, crack = view.res, view.res.crack
        assert view.choice.currentData() == str(folder)
        assert int(res.frames.index[crack.estimate]) == 305
        assert "CRACK" in view.info.text()
        assert "Break load" in view.crack_text.text()

        view.go(0)
        view.jump_to_crack()
        assert view.pos == crack.estimate and view.slider.value() == crack.estimate

        view.crack_only.setChecked(True)
        assert (view.slider.minimum(), view.slider.maximum()) == (crack.start, crack.end)
        view.go(crack.end)
        view._play_step()                                         # loops the crack window
        assert view.pos == crack.start

        view.play_btn.click()
        start = view.pos
        assert _wait(app, lambda: view.pos != start, 3)
        view.stop_play()

        assert view.plot_list.count() == 2
        assert "Peak load" in view.summary.toPlainText()

        # A test starts: the tab is greyed out and the view moves to another tab.
        win.runner = _Runner()
        assert _wait(app, lambda: not win.plots.isTabEnabled(win.results_index), 2)
        assert win.plots.currentWidget() is not view and not view.timer.isActive()
        win.show_results(folder)                                   # refused
        assert win.plots.currentWidget() is not view
        win.runner = None
        assert _wait(app, lambda: win.plots.isTabEnabled(win.results_index), 2)
    finally:
        win.runner = None
        win.close()
        app.processEvents()
