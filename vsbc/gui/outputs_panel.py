"""Outputs tab: the disk space, every test and camera recording stored here,
copying them to a USB drive, and freeing space.

The listing and the copy run on worker threads (sizing thousands of camera
frames takes a moment). A copy pauses while a recording runs, so the camera
gets the disk to itself.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from vsbc.gui.camera_panel import narrow_combo
from vsbc.gui.common import QtCore, QtGui, QtWidgets, button
from vsbc.gui.panels import Panel
from vsbc.storage import (
    copy_outputs, copy_size, delete_entry, delete_images, delete_old_logs, eject, human_size, list_outputs,
    removable_drives, remaining_tests,
)

COLUMNS = ("Started", "Specimen / name", "Result", "Size", "Camera images", "Video", "DIC")
LEVEL_STYLE = {
    "ok": "QProgressBar::chunk { background: #1e8449; }",
    "low": "QProgressBar::chunk { background: #d68910; }",
    "critical": "QProgressBar::chunk { background: #c0392b; }",
}


class OutputsPanel(Panel):
    def __init__(self, win):
        super().__init__(win)
        self.entries = []
        self.status = None
        self._copy_cancel = threading.Event()
        self.copy_pause = threading.Event()        # set = paused (a recording is running)
        self._copying = False
        self._listing = False

        storage_box = QtWidgets.QGroupBox("Storage")
        self.path_label = QtWidgets.QLabel(str(win.config.output_dir))
        self.path_label.setWordWrap(True)
        self.usage = QtWidgets.QProgressBar()
        self.usage.setRange(0, 1000)
        self.usage.setTextVisible(True)
        self.usage_text = QtWidgets.QLabel()
        self.usage_text.setWordWrap(True)
        self.breakdown = QtWidgets.QLabel()
        self.breakdown.setWordWrap(True)
        s = QtWidgets.QVBoxLayout(storage_box)
        s.addWidget(self.path_label)
        s.addWidget(self.usage)
        s.addWidget(self.usage_text)
        s.addWidget(self.breakdown)

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(220)
        self.table.setMinimumWidth(100)        # scrolls sideways instead of widening the tab
        self.table.itemSelectionChanged.connect(self._selection_changed)

        self.refresh_btn = button("Refresh", self.refresh)
        self.open_btn = button("Open folder", self.open_selected)
        self.delete_images_btn = button("Delete camera images...", self.delete_images)
        self.delete_images_btn.setToolTip("Frees most of the space: keeps data.csv, the video, DIC results and plots")
        self.delete_btn = button("Delete...", self.delete_selected)
        self.logs_btn = button("Delete logs older than 30 days", self.delete_logs)
        actions = QtWidgets.QGridLayout()
        actions.addWidget(self.refresh_btn, 0, 0)
        actions.addWidget(self.open_btn, 0, 1)
        actions.addWidget(self.delete_images_btn, 1, 0)
        actions.addWidget(self.delete_btn, 1, 1)

        usb_box = QtWidgets.QGroupBox("Copy to a USB drive")
        self.drives = narrow_combo(QtWidgets.QComboBox())
        self.find_btn = button("⟳", self.find_drives)
        self.find_btn.setFixedWidth(48)
        self.find_btn.setToolTip("Look for USB drives again")
        self.choose_btn = button("Other folder...", self.choose_folder)
        self.include_images = QtWidgets.QCheckBox("Include the raw camera images")
        self.include_images.setToolTip("Usually most of the size. data.csv, the video, DIC results and plots "
                                       "are always copied.")
        self.copy_btn = button("Copy the selected tests", self.copy_selected)
        self.cancel_copy_btn = button("Cancel", self._copy_cancel.set)
        self.eject_btn = button("Eject", self.eject_drive)
        self.copy_progress = QtWidgets.QProgressBar()
        self.copy_progress.setRange(0, 1000)
        self.copy_text = QtWidgets.QLabel()
        self.copy_text.setWordWrap(True)
        u = QtWidgets.QVBoxLayout(usb_box)
        drive_row = QtWidgets.QHBoxLayout()
        drive_row.addWidget(self.drives, 1)
        drive_row.addWidget(self.find_btn)
        drive_row.addWidget(self.choose_btn)
        u.addLayout(drive_row)
        u.addWidget(self.include_images)
        copy_row = QtWidgets.QHBoxLayout()
        copy_row.addWidget(self.copy_btn, 1)
        copy_row.addWidget(self.cancel_copy_btn)
        copy_row.addWidget(self.eject_btn)
        u.addLayout(copy_row)
        u.addWidget(self.copy_progress)
        u.addWidget(self.copy_text)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(storage_box)
        layout.addWidget(QtWidgets.QLabel("Tests and camera recordings on this computer (newest first):"))
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(usb_box)
        layout.addWidget(self.logs_btn)
        self._selection_changed()
        self.find_drives()

    # ---------------------------------------------------------------- disk --
    def show_disk(self, status) -> None:
        self.status = status
        self.usage.setValue(int(1000 * status.used_fraction))
        self.usage.setFormat(f"{100 * status.used_fraction:.0f} % used")
        self.usage.setStyleSheet(LEVEL_STYLE[status.level])
        text = status.text()
        bytes_per_test = self.win.camera_panel.test_bytes(self.win.camera_panel._test_duration_s()) \
            if self.win.camera is not None else 0.0
        fits = remaining_tests(status.free, bytes_per_test)
        if fits is not None:
            text += f". About {fits} more test(s) with the camera at the current settings."
        if status.level != "ok":
            text = ("RUNNING OUT OF SPACE: " if status.level == "critical" else "Space is getting low: ") + text
        self.usage_text.setText(text)

    # -------------------------------------------------------------- listing --
    def refresh(self) -> None:
        if self._listing:
            return
        self._listing = True
        config = self.win.config

        def work():
            entries = list_outputs(config.output_dir)
            logs = sum(p.stat().st_size for p in config.logs_dir.glob("*") if p.is_file()) \
                if config.logs_dir.is_dir() else 0
            return entries, logs

        def done(result):
            self._listing = False
            entries, logs = result
            self.entries = entries
            self._fill(entries)
            tests = sum(e.size for e in entries if e.kind == "test")
            recordings = sum(e.size for e in entries if e.kind == "recording")
            images = sum(e.images_size for e in entries)
            dic = sum(e.dic_size for e in entries)
            self.breakdown.setText(f"Tests: {human_size(tests)} ({sum(e.kind == 'test' for e in entries)}); "
                                   f"camera recordings without a test: {human_size(recordings)}; of all that, camera "
                                   f"images {human_size(images)} and DIC results {human_size(dic)}; "
                                   f"logs {human_size(logs)}.")
            self.win.check_storage()

        def failed(exc):
            self._listing = False
            self.win.warn("Outputs", str(exc))

        self.win.run_async(work, done, failed, busy=False)

    def _fill(self, entries) -> None:
        self.table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            try:
                started = datetime.fromisoformat(e.started).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                started = e.started
            result = e.stop_reason or e.status or ("recording" if e.kind == "recording" else "")
            if e.kind == "recording":
                result = f"camera only{', ' + e.status if e.status and e.status != 'complete' else ''}"
            images = ("deleted" if e.images_deleted else
                      f"{e.n_images} ({human_size(e.images_size)})" if e.n_images else "—")
            cells = (started, e.name, result, human_size(e.size), images, "yes" if e.has_video else "—",
                     f"{e.n_dic} frames" if e.n_dic else "—")
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, col, item)
        self._selection_changed()

    def selected(self) -> list:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.entries[r] for r in rows if r < len(self.entries)]

    def _busy_paths(self) -> set[str]:
        """Folders in use: the test running now, the recording, the one being post-processed."""
        busy = set()
        runner = self.win.runner
        if runner is not None and runner.recorder is not None:
            busy.add(str(runner.recorder.folder))
        cam = self.win.camera
        if cam is not None and cam.recording is not None:
            folder = Path(cam.recording["folder"])
            busy.update({str(folder), str(folder.parent)})
        job = self.win.postprocess.current
        if job is not None:
            busy.update({str(job.run), str(job.run.parent)})
        return busy

    def _selection_changed(self) -> None:
        chosen = self.selected()
        busy = self._busy_paths()
        free = bool(chosen) and not any(str(e.path) in busy for e in chosen)
        self.open_btn.setEnabled(len(chosen) == 1)
        self.delete_btn.setEnabled(free)
        self.delete_images_btn.setEnabled(free and any(e.n_images for e in chosen))
        self.copy_btn.setEnabled(bool(chosen) and not self._copying and self.drives.count() > 0
                                 and self.drives.currentData() is not None)

    def open_selected(self) -> None:
        chosen = self.selected()
        if chosen:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(chosen[0].path)))

    # ------------------------------------------------------------- deleting --
    def delete_images(self) -> None:
        chosen = [e for e in self.selected() if e.n_images]
        if not chosen:
            return
        total = sum(e.images_size for e in chosen)
        no_dic = [e.name for e in chosen if not e.n_dic]
        text = (f"Delete the raw camera images of {len(chosen)} recording(s), {human_size(total)}?\n\n"
                "data.csv, the video, the DIC results and the plots are kept.")
        if no_dic:
            text += ("\n\nDIC has not been run on: " + ", ".join(no_dic[:5]) +
                     ("..." if len(no_dic) > 5 else "") + ". Without the images it never can be.")
        if not self.win.ask("Delete camera images", text):
            return
        self.win.run_async(lambda: sum(delete_images(e) for e in chosen),
                           lambda freed: (self.win.status(f"Freed {human_size(freed)}"), self.refresh()))

    def delete_selected(self) -> None:
        chosen = self.selected()
        if not chosen:
            return
        total = sum(e.size for e in chosen)
        names = ", ".join(e.name for e in chosen[:5]) + ("..." if len(chosen) > 5 else "")
        if not self.win.ask("Delete", f"Permanently delete {len(chosen)} folder(s) ({names}), {human_size(total)}?\n\n"
                                      "This cannot be undone. Copy them to a USB drive first if you need them."):
            return
        output_dir = self.win.config.output_dir
        self.win.run_async(lambda: sum(delete_entry(e, output_dir) for e in chosen),
                           lambda freed: (self.win.status(f"Deleted, {human_size(freed)} freed"), self.refresh()))

    def delete_logs(self) -> None:
        if not self.win.ask("Delete logs", "Delete log files not changed in the last 30 days?"):
            return
        self.win.run_async(lambda: delete_old_logs(self.win.config.logs_dir, 30.0),
                           lambda r: (self.win.status(f"Deleted {r[0]} log file(s), {human_size(r[1])}"),
                                      self.refresh()))

    # -------------------------------------------------------------- copying --
    def find_drives(self) -> None:
        current = self.drives.currentData()
        self.drives.clear()
        try:
            drives = removable_drives()
        except OSError:
            drives = []
        for d in drives:
            self.drives.addItem(d.text(), str(d.path))
        if not drives:
            self.drives.addItem("No USB drive: plug one in, press ⟳", None)
        index = self.drives.findData(current) if current else -1
        if index >= 0:
            self.drives.setCurrentIndex(index)
        self._selection_changed()

    def choose_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Copy the outputs to", str(Path.home()))
        if path:
            self.drives.insertItem(0, path, path)
            self.drives.setCurrentIndex(0)
            self._selection_changed()

    def copy_selected(self) -> None:
        chosen = self.selected()
        destination = self.drives.currentData()
        if not chosen or not destination:
            return
        include = self.include_images.isChecked()
        size = copy_size(chosen, include)
        if not self.win.ask("Copy", f"Copy {len(chosen)} folder(s), {human_size(size)}, to {destination}"
                                    f"{' with' if include else ' without'} the camera images?"):
            return
        self._copying = True
        self._copy_cancel.clear()
        self.copy_progress.setValue(0)
        output_dir = self.win.config.output_dir

        def progress(done: int, total: int, name: str) -> None:
            self.win.post(self._show_progress, done, total, name)

        def work():
            return copy_outputs(chosen, output_dir, Path(destination), include, progress, self._copy_cancel,
                                self.copy_pause)

        def finished(result):
            self._copying = False
            self._selection_changed()
            if result.cancelled:
                self.copy_text.setText(f"Cancelled after {result.files} files ({human_size(result.bytes)}).")
            elif result.errors:
                self.copy_text.setText(f"Copied {result.files} files with {len(result.errors)} problem(s): "
                                       f"{result.errors[0]}")
            else:
                self.copy_progress.setValue(1000)
                self.copy_text.setText(f"Copied {result.files} files ({human_size(result.bytes)}) to "
                                       f"{result.destination}. Press Eject before unplugging the drive.")

        def failed(exc):
            self._copying = False
            self._selection_changed()
            self.copy_text.setText(f"Copy failed: {exc}")

        self.win.run_async(work, finished, failed, busy=False)
        self._selection_changed()

    def _show_progress(self, done: int, total: int, name: str) -> None:
        self.copy_progress.setValue(int(1000 * done / total) if total else 1000)
        paused = " (paused while the camera records)" if self.copy_pause.is_set() else ""
        self.copy_text.setText(f"{human_size(done)} of {human_size(total)}{paused}: {name}")

    def eject_drive(self) -> None:
        destination = self.drives.currentData()
        if destination and not self._copying:
            self.win.run_async(lambda: eject(Path(destination)),
                               lambda message: (self.copy_text.setText(message), self.find_drives()), busy=False)

    def update_enabled(self, connected: bool, busy: bool) -> None:
        self.cancel_copy_btn.setEnabled(self._copying)
        self.eject_btn.setEnabled(not self._copying and self.drives.currentData() is not None)
