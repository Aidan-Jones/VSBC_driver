"""Screen-size scaling: text size, wrapping button rows and tabs, no sideways scrolling."""

import os

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vsbc.gui import common  # noqa: E402
from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import FlowLayout, FlowTabs, QtWidgets, font_px_for  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def restore_font(app):
    font, sheet, px = app.font(), app.styleSheet(), common._font_px
    yield
    app.setFont(font)
    app.setStyleSheet(sheet)
    common._font_px = px


def test_text_size_follows_the_screen():
    reference = 11 * 96 / 72
    assert font_px_for(1280, 800, 11) == pytest.approx(reference)
    assert font_px_for(1920, 1080, 11) == pytest.approx(reference * 1.35)       # height limits: 1080 / 800
    assert font_px_for(3840, 2160, 11) == pytest.approx(reference * 2.7)
    # Small screens keep readable text instead of shrinking with the screen.
    assert font_px_for(800, 480, 11) == common.MIN_FONT_PX
    assert font_px_for(1024, 600, 11) == common.MIN_FONT_PX
    # gui.scale fixes the factor.
    assert font_px_for(800, 480, 11, scale=1.5) == pytest.approx(reference * 1.5)


def test_apply_style_sizes_everything_from_the_text(app, restore_font):
    px = common.apply_style(app, 11, size=(1920, 1080))
    assert app.font().pixelSize() == round(px)
    assert common.em(2) == round(2 * px)
    assert f"min-height: {common.em(2.3)}px" in app.styleSheet()


def test_flow_layout_wraps_when_narrow(app):
    host = QtWidgets.QWidget()
    layout = FlowLayout(host)
    buttons = [QtWidgets.QPushButton(f"Button number {i}") for i in range(6)]
    for b in buttons:
        layout.addWidget(b)
    one_row = layout.sizeHint()
    assert layout.heightForWidth(one_row.width()) == pytest.approx(one_row.height(), abs=1)
    narrow = max(b.sizeHint().width() for b in buttons)
    assert layout.heightForWidth(narrow) >= 5 * buttons[0].sizeHint().height()
    host.resize(narrow, layout.heightForWidth(narrow))
    layout.setGeometry(host.rect())
    assert all(b.geometry().width() == narrow for b in buttons)     # stretched to the full row


def test_flow_tabs_act_like_a_tab_widget(app):
    tabs = FlowTabs()
    seen = []
    tabs.currentChanged.connect(seen.append)
    inner = QtWidgets.QLabel("inside")
    area = QtWidgets.QScrollArea()
    area.setWidget(inner)
    assert tabs.addTab(QtWidgets.QLabel("a"), "A") == 0
    assert tabs.addTab(area, "B") == 1
    assert tabs.insertTab(0, QtWidgets.QLabel("z"), "Z && Co") == 0
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Z && Co", "A", "B"]
    tabs.setCurrentWidget(inner)                   # a panel inside its scroll area finds its tab
    assert tabs.currentIndex() == 2 and tabs.currentWidget() is area
    assert seen[-1] == 2
    tabs.setTabText(2, "B ⚠")
    assert tabs.tabText(2) == "B ⚠"
    assert [b.isChecked() for b in tabs._buttons] == [False, False, True]


def _real_font(app) -> bool:
    """DejaVu Sans (matplotlib ships it; Raspberry Pi OS uses it too), so text has real widths."""
    from pathlib import Path

    import matplotlib

    from vsbc.gui.common import QtGui

    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    ids = [QtGui.QFontDatabase.addApplicationFont(str(ttf / name)) for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")]
    if min(ids) < 0:
        return False
    font = QtGui.QFont("DejaVu Sans")
    font.setPixelSize(13)
    app.setFont(font)
    # Real: about 68 px. A platform without fonts gives every letter a full em (130 px).
    return QtGui.QFontMetrics(font).horizontalAdvance("abcdefghij") < 100


@pytest.mark.parametrize("size", [(800, 480), (1280, 720)])
def test_window_fits_small_screens_without_sideways_scrolling(app, config, restore_font, size):
    if not _real_font(app):
        pytest.skip("this Qt platform has no usable fonts, so text widths are not real")
    config.camera.simulate = True
    common.apply_style(app, 11, size=size)
    win = MainWindow(config, interactive=False)
    try:
        win.resize(*size)
        win.show()
        for _ in range(10):
            app.processEvents()
        for i in range(win.tabs.count()):
            win.tabs.setCurrentIndex(i)
            app.processEvents()
            page = win.tabs.widget(i)
            if isinstance(page, QtWidgets.QScrollArea):
                assert not page.horizontalScrollBar().isVisible()
                assert page.widget().width() <= page.viewport().width()
        win.tabs.setCurrentWidget(win.firmware_panel)
        assert win.tabs.currentIndex() == win.tabs.count() - 1
    finally:
        win.close()
        app.processEvents()
