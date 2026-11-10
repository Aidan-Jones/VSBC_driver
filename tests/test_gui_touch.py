"""Touch scrolling: drag a page to scroll it, a tap still clicks, plots keep their own drag."""

import os
import time

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vsbc.gui import common  # noqa: E402
from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import QtCore, QtWidgets, touch_scroll_wanted  # noqa: E402
from pyqtgraph.Qt import QtTest  # noqa: E402

Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def _at(win, widget, pos):
    """A point in `widget` as the window's coordinates (events go in through the window, like real input)."""
    return widget.mapTo(win, pos)


def _drag(app, win, widget, start, dy, steps=12, step_s=0.012):
    w, left = win.windowHandle(), Qt.MouseButton.LeftButton
    QtTest.QTest.mousePress(w, left, Qt.KeyboardModifier.NoModifier, _at(win, widget, start))
    for k in range(1, steps + 1):
        _pump(app, step_s)
        QtTest.QTest.mouseMove(w, _at(win, widget, start + QtCore.QPoint(0, round(dy * k / steps))))
    QtTest.QTest.mouseRelease(w, left, Qt.KeyboardModifier.NoModifier,
                              _at(win, widget, start + QtCore.QPoint(0, dy)))


def _tap(app, win, widget, pos, hold=0.03):
    w, left = win.windowHandle(), Qt.MouseButton.LeftButton
    QtTest.QTest.mousePress(w, left, Qt.KeyboardModifier.NoModifier, _at(win, widget, pos))
    _pump(app, hold)
    QtTest.QTest.mouseRelease(w, left, Qt.KeyboardModifier.NoModifier, _at(win, widget, pos))


def test_setting():
    assert touch_scroll_wanted(True) and touch_scroll_wanted("on")
    assert not touch_scroll_wanted(False) and not touch_scroll_wanted("off")
    assert touch_scroll_wanted("auto") == common.has_touch_screen()


def test_values_are_in_pixels_whatever_dpi_the_screen_reports(app):
    widget = QtWidgets.QWidget()
    props = common.touch_scroller_properties(widget)
    m = QtWidgets.QScrollerProperties.ScrollMetric
    screen = widget.screen() or QtWidgets.QApplication.primaryScreen()
    dpi = screen.physicalDotsPerInchY()
    px_per_m = (dpi if dpi > 1 else 96.0) / 0.0254
    dp = common._font_px / 14.0
    assert props.scrollMetric(m.DragStartDistance) * px_per_m == pytest.approx(10 * dp)
    assert props.scrollMetric(m.MaximumVelocity) * px_per_m == pytest.approx(3000 * dp)
    assert props.scrollMetric(m.MousePressEventDelay) == pytest.approx(0.1)


@pytest.fixture
def window(app, config):
    config.gui.touch_scroll = True
    win = MainWindow(config, interactive=False)
    win.resize(1100, 560)              # short: the Test page has to scroll
    win.show()
    # A delayed tap is replayed to QApplication.widgetAt() under the finger, so the right panel must be on
    # the (800 x 600) offscreen screen; on the Pi the window is on its screen anyway.
    win.move(780 - win.width(), 0)
    _pump(app, 0.2)
    yield win
    win.close()
    app.processEvents()


def test_drag_scrolls_a_page_and_tap_still_clicks(app, window):
    win = window
    page = win.tabs.widget(0)                       # the Test tab's scroll area
    bar = page.verticalScrollBar()
    assert QtWidgets.QScroller.hasScroller(page.viewport()) and bar.maximum() > 0
    bar.setValue(0)
    # Plots and the camera view keep their own drag (pan, zoom, ROI).
    assert not QtWidgets.QScroller.hasScroller(win.plots.test_plot.viewport())
    assert not QtWidgets.QScroller.hasScroller(win.camera_view.view.viewport())

    # A drag anywhere on the page scrolls it.
    vp = page.viewport()
    _drag(app, win, vp, vp.rect().center(), -150)
    _pump(app, 1.5)                                 # the fling and its bounce settle
    assert bar.value() > 0

    # A drag that starts on a button scrolls too, and does not press the button.
    header = win.test_panel.stops.header
    page.ensureWidgetVisible(header, 0, vp.height() // 3)
    _pump(app, 0.2)
    expanded = win.test_panel.stops.is_expanded()
    before = bar.value()
    start = header.mapTo(vp, header.rect().center())
    assert vp.rect().contains(start)
    _drag(app, win, vp, start, 120 if before > 0 else -120)
    _pump(app, 1.5)
    assert bar.value() != before
    assert win.test_panel.stops.is_expanded() == expanded

    # A tap (no movement) still clicks: the delayed press and the release both arrive. So does a slow press
    # (held longer than the 0.1 s press delay) that does not move.
    for hold in (0.03, 0.4):
        page.ensureWidgetVisible(header, 0, vp.height() // 3)
        _pump(app, 0.2)
        before = win.test_panel.stops.is_expanded()
        _tap(app, win, header, header.rect().center(), hold)
        _pump(app, 0.3)
        assert win.test_panel.stops.is_expanded() != before, hold


def test_outputs_list_selects_by_tapping(app, window):
    table = window.outputs_panel.table
    assert table.selectionMode() == QtWidgets.QAbstractItemView.SelectionMode.MultiSelection
