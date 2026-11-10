"""The DIC tab, offscreen, on a synthetic test with DIC, strain and crack-tip files."""

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import vsbc.gui.dic_view as dic_view_module  # noqa: E402
from vsbc.camera import dic_results as dr  # noqa: E402
from vsbc.camera.postprocess import Job, PostProcessor  # noqa: E402
from vsbc.camera.roi import Roi, load_roi, rect_mask  # noqa: E402
from vsbc.gui.app import MainWindow  # noqa: E402
from vsbc.gui.common import QtWidgets  # noqa: E402
from tests.test_dic_results import ENV_OK, write_dic, write_strain  # noqa: E402
from tests.test_results import SHAPE, make_test  # noqa: E402


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
    recorder = None

    def request_stop(self, *_args):
        pass


@pytest.fixture
def window(app, config, monkeypatch):
    monkeypatch.setattr(dic_view_module, "_env_check", ENV_OK)      # no camera environment here
    test = make_test(config.output_dir / "tests" / "2026-11-05", name="120000_T1")
    run = test / "camera"
    for f in range(296, 312):
        write_dic(run, f, v_scale=1 + (f - 296) / 10)
        write_strain(run, f, eyy=1e-3 * (f - 295))
    (run / "crack_tip_image.csv").write_text(
        "frame_index,time_s,crack_tip_x_px,crack_tip_y_px,crack_length_px\n"
        "305,3.05,20,20,15\n306,3.06,30,20,25\n", encoding="utf-8")
    win = MainWindow(config, interactive=False)
    win.resize(1280, 800)
    win.show()
    win.test_folder = test
    yield win
    win.runner = None
    win.close()
    app.processEvents()


def _open(app, win):
    view = win.dic_view
    win.show_dic()                     # Results > DIC analysis, following the Results tab's test picker
    assert win.left_tabs.currentWidget() is win.results_tab and win.results_tab.inner.currentWidget() is view
    assert _wait(app, lambda: view.data is not None and view.dic_pane.frames, 10)
    # It shows the test chosen in the Results tab's picker (one picker for Overview and DIC analysis).
    assert Path(view.data.res.folder) == win.results_view.current_folder()
    return view


def test_dic_tab_validates_and_runs(app, window, monkeypatch):
    win = window
    assert win.results_tab.inner.tabText(1) == "DIC analysis" and win.results_tab.inner.widget(1) is win.dic_view
    view = _open(app, win)
    pane = view.run_pane
    pane.run_strain.setChecked(True)
    pane.run_dic.setChecked(True)
    assert dr.can_run(pane.checks), [c.text for c in pane.checks]
    assert pane.run_btn.isEnabled()
    queued = []
    monkeypatch.setattr(win, "queue_postprocess", lambda folder, options=None: queued.append((folder, options)))
    pane.redo.setChecked(True)
    pane.run_btn.click()
    folder, options = queued[0]
    assert Path(folder) == win.test_folder / "camera" and options.run_dic and options.redo
    assert not options.make_video

    # Nothing ticked: Run is greyed out and the checklist says why.
    for box in (pane.run_dic, pane.run_strain, pane.crack_image, pane.crack_opening):
        box.setChecked(False)
    assert not pane.run_btn.isEnabled() and "Nothing to run" in pane.check_text.text()


def test_dic_strain_and_crack_views(app, window):
    win = window
    view = _open(app, win)
    dic = view.dic_pane
    assert dic.frames == list(range(296, 312))
    assert _wait(app, lambda: dic.curve.getData()[0] is not None and len(dic.curve.getData()[0]) == 16, 5)
    dic.go(5)
    assert dic.overlay.isVisible() and "converged" in dic.info.text()
    dic.component.setCurrentIndex(dic.component.findData("mag"))
    assert _wait(app, lambda: dic.limits[0] > 0, 5)

    strain = view.strain_pane
    assert strain.frames == list(range(296, 312))
    assert strain.component.currentData() == "eps_11"
    strain.go(3)
    assert strain.overlay.isVisible() and "µε" in strain.info.text()

    crack = view.crack_pane
    assert crack.frames and min(crack.frames) == 250                # the full-rate window
    crack.go_frame(305)
    assert "image tracker" in crack.info.text()
    crack.mark(305, 25.0, 21.0)
    assert dr.read_manual(win.test_folder / "camera") == {305: (25.0, 21.0)}
    assert "by hand" in crack.info.text() and "1 marked by hand" in crack.note.text()
    crack.delete_mark()
    assert dr.read_manual(win.test_folder / "camera") == {}

    view.inner.setCurrentIndex(4)                                 # Log
    assert "No post-processing log" in view.log_text.toPlainText()


def test_missing_roi_offers_the_current_one(app, window):
    win = window
    run = win.test_folder / "camera"
    (run / "roi.npz").unlink()
    import json
    settings = json.loads((run / "capture_settings.json").read_text(encoding="utf-8"))
    settings.update(height=SHAPE[0], width=SHAPE[1])
    (run / "capture_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    win.roi = Roi(rect_mask(SHAPE, 2, 2, 60, 38), seed=(30, 20), notch=(4, 20))
    view = _open(app, win)
    assert _wait(app, lambda: view.run_pane.fix_btn.isVisibleTo(view.run_pane), 5)
    view.run_pane.fix_btn.click()
    assert load_roi(run / "roi.npz") is not None
    assert _wait(app, lambda: view.data is not None and view.data.res.roi is not None
                 and not view.run_pane.fix_btn.isVisibleTo(view.run_pane), 5)


def test_dic_tab_is_greyed_out_during_a_test(app, window):
    win = window
    view = _open(app, win)
    win.runner = _Runner()
    assert _wait(app, lambda: not win.left_tabs.isTabEnabled(win.results_index), 2)
    assert win.left_tabs.currentWidget() is not win.results_tab and not view.available
    win.runner = None
    assert _wait(app, lambda: win.left_tabs.isTabEnabled(win.results_index), 2)


def test_cancel_run_leaves_other_tests_alone(config, tmp_path):
    pp = PostProcessor(config)
    a, b = tmp_path / "a", tmp_path / "b"
    pp.add([Job("a: DIC", "dic", [], a), Job("b: DIC", "dic", [], b), Job("a: strain", "strain", [], a)])
    assert pp.running_for(a) and pp.running_for(b)
    pp.cancel_run(a)
    assert [j.title for j in pp.queue] == ["b: DIC"] and not pp.running_for(a)


def test_draw_this_tests_roi_in_a_pop_up(app, window):
    win = window
    run = win.test_folder / "camera"
    (run / "roi.npz").unlink()
    view = _open(app, win)
    pane = view.run_pane
    assert _wait(app, lambda: pane.draw_roi_btn.isEnabled(), 5)
    assert pane.draw_roi_btn.text() == "Draw ROI on a frame..."
    assert any(c.level == "error" and "Draw ROI" in c.text for c in pane.checks)
    pane.redo.setChecked(False)
    pane.draw_roi_btn.click()
    dialog = pane.roi_dialog
    assert dialog is not None and dialog.isVisible() and dialog.shape == SHAPE
    assert dialog.frames[0] == 0 and "DIC reference" in dialog.frame_label.text()
    dialog._to_break()                                            # scrub to the break and back
    assert dialog.slider.value() > 0
    dialog.slider.setValue(0)
    dialog.set_box(6, 4, 58, 36)
    dialog.place("seed", 30, 20)
    dialog.place("notch", 8, 20)
    assert dialog.save()
    roi = load_roi(run / "roi.npz")
    assert roi is not None and roi.shape == SHAPE and roi.seed == (30, 20) and roi.notch == (8, 20)
    assert roi.mask[20, 30] and not roi.mask[1, 1]
    assert (run / "roi_snapshot.tif").is_file()
    assert win.roi is None and not win.roi_file.exists()          # the Camera tab's ROI is left alone
    assert pane.redo.isChecked()                                  # DIC results were made without this ROI
    assert _wait(app, lambda: view.data is not None and view.data.res.roi is not None
                 and pane.draw_roi_btn.text() == "Edit this test's ROI...", 5)
    assert not any(c.level == "error" and "ROI" in c.text for c in pane.checks)
    assert any(c.text.startswith("Notch tip at x 8") for c in pane.checks)

    # Editing it again starts from this test's ROI; a seed outside the ROI is moved inside.
    pane.draw_roi_btn.click()
    dialog = pane.roi_dialog
    assert dialog.start.currentData() == "test" and dialog.seed == (30, 20)
    dialog.set_box(40, 4, 60, 36)
    assert dialog.save()
    roi = load_roi(run / "roi.npz")
    assert roi.mask[roi.seed[1], roi.seed[0]] and roi.seed[0] >= 40
