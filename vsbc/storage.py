"""Disk space and the test outputs on this computer (the Outputs tab).

Pure functions, safe to call on worker threads: the free space on the drive
that holds the outputs (with warning levels), a listing of every test and
camera recording with its size, copying them to a USB drive, ejecting it, and
deleting a test or only its raw camera images.
"""

from __future__ import annotations

import ctypes
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from vsbc.share import upload_path

log = logging.getLogger(__name__)

GB = 1024 ** 3
COPY_FOLDER = "VSBC_outputs"          # copies go to <drive>/VSBC_outputs/tests/<date>/<test>/
IMAGES_DELETED_FILE = "images_deleted.txt"


def human_size(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# --------------------------------------------------------------- disk space --

@dataclass(frozen=True)
class DiskStatus:
    path: Path
    total: int
    used: int
    free: int
    level: str                        # "ok", "low" or "critical"

    @property
    def free_gb(self) -> float:
        return self.free / GB

    @property
    def used_fraction(self) -> float:
        return self.used / self.total if self.total else 0.0

    def text(self) -> str:
        return (f"{human_size(self.free)} free of {human_size(self.total)} "
                f"({100 * self.used_fraction:.0f} % used)")


def disk_status(path: Path, warn_free_gb: float = 10.0, critical_free_gb: float = 2.0) -> DiskStatus:
    """Free space on the drive holding `path` (or its nearest existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    free_gb = usage.free / GB
    level = "critical" if free_gb < critical_free_gb else "low" if free_gb < warn_free_gb else "ok"
    return DiskStatus(Path(path), usage.total, usage.used, usage.free, level)


def remaining_tests(free_bytes: int, bytes_per_test: float, margin_gb: float = 1.0) -> int | None:
    """How many more tests of this size fit, keeping a margin free."""
    if bytes_per_test <= 0:
        return None
    return max(0, int((free_bytes - margin_gb * GB) // bytes_per_test))


# ------------------------------------------------------------------ listing --

@dataclass
class OutputEntry:
    path: Path
    kind: str                         # "test", or "recording" (camera only, made without a test)
    started: str                      # ISO time
    name: str                         # specimen ID or recording name
    status: str = ""                  # complete / aborted / running
    stop_reason: str = ""
    size: int = 0
    images_size: int = 0
    n_images: int = 0
    dic_size: int = 0
    n_dic: int = 0
    has_video: bool = False
    has_plots: bool = False
    images_deleted: bool = False
    has_upload: bool = False          # Outputs/upload/<date>/<name>.zip exists

    @property
    def camera_dir(self) -> Path:
        return self.path / "camera" if self.kind == "test" else self.path

    @property
    def images_dir(self) -> Path:
        return self.camera_dir / "images"

    @property
    def dic_dir(self) -> Path:
        return self.camera_dir / "pyvale_results"

    @property
    def has_camera(self) -> bool:
        return self.camera_dir.is_dir() and (self.camera_dir / "frames.csv").is_file()


_size_cache: dict[str, tuple[tuple, tuple]] = {}
_cache_lock = threading.Lock()


def _scan(path: Path) -> tuple[int, int]:
    """(bytes, files) under path."""
    total = files = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _sizes(entry: OutputEntry, use_cache: bool) -> tuple[int, int, int, int, int]:
    """(total, images, n_images, dic, n_dic). Cached by the folders' modification times."""
    key = str(entry.path)
    signature = tuple(_mtime(p) for p in (entry.path, entry.camera_dir, entry.images_dir, entry.dic_dir))
    with _cache_lock:
        cached = _size_cache.get(key)
    if use_cache and cached and cached[0] == signature:
        return cached[1]
    total, _files = _scan(entry.path)
    images, n_images = _scan(entry.images_dir) if entry.images_dir.is_dir() else (0, 0)
    dic, n_dic = _scan(entry.dic_dir) if entry.dic_dir.is_dir() else (0, 0)
    sizes = (total, images, n_images, dic, n_dic)
    with _cache_lock:
        _size_cache[key] = (signature, sizes)
    return sizes


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _started_from_folder(folder: Path) -> str:
    """'2026-09-29' + '153012_A1' -> '2026-09-29T15:30:12'."""
    try:
        return datetime.strptime(f"{folder.parent.name} {folder.name[:6]}", "%Y-%m-%d %H%M%S").isoformat()
    except ValueError:
        return datetime.fromtimestamp(_mtime(folder) / 1e9).isoformat(timespec="seconds")


def list_outputs(output_dir: Path) -> list[OutputEntry]:
    """Every test (Outputs/tests/<date>/<test>) and camera recording (Outputs/camera/<date>/<name>), newest first."""
    output_dir = Path(output_dir)
    entries: list[OutputEntry] = []
    for kind, root, marker_files in (("test", output_dir / "tests", ("data.csv", "meta.json")),
                                     ("recording", output_dir / "camera", ("frames.csv", "capture_settings.json"))):
        if not root.is_dir():
            continue
        for day in sorted(root.iterdir()):
            if not day.is_dir():
                continue
            for folder in day.iterdir():
                if not folder.is_dir() or not any((folder / m).exists() for m in marker_files + ("images",)):
                    continue
                if kind == "test":
                    meta = _read_json(folder / "meta.json")
                    spec = meta.get("specimen") or {}
                    entry = OutputEntry(folder, kind, meta.get("started") or _started_from_folder(folder),
                                        spec.get("specimen_id") or folder.name[7:], meta.get("status", ""),
                                        meta.get("stop_reason") or "")
                else:
                    settings = _read_json(folder / "capture_settings.json")
                    entry = OutputEntry(folder, kind, settings.get("started") or _started_from_folder(folder),
                                        settings.get("test_name") or folder.name[7:],
                                        settings.get("status", "complete"), "")
                entries.append(entry)
    for entry in entries:
        entry.size, entry.images_size, entry.n_images, entry.dic_size, entry.n_dic = _sizes(
            entry, use_cache=entry.status != "running")
        entry.has_video = (entry.path / "video.mp4").is_file()
        entry.has_plots = (entry.camera_dir / "plots").is_dir() or (entry.path / "plot.png").is_file()
        entry.images_deleted = (entry.camera_dir / IMAGES_DELETED_FILE).is_file()
        entry.has_upload = upload_path(entry.path, output_dir).is_file()
    entries.sort(key=lambda e: e.started, reverse=True)
    return entries


# ------------------------------------------------------------------ copying --

@dataclass
class CopyResult:
    destination: Path
    files: int = 0
    bytes: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def _copy_plan(entries: list[OutputEntry], output_dir: Path, include_images: bool) -> list[tuple[Path, Path, int]]:
    plan = []
    for entry in entries:
        try:
            rel_root = entry.path.resolve().relative_to(Path(output_dir).resolve())
        except ValueError:
            rel_root = Path(entry.path.name)
        skip = entry.images_dir.resolve() if not include_images else None
        for dirpath, dirnames, filenames in os.walk(entry.path):
            here = Path(dirpath)
            if skip is not None and here.resolve() == skip:
                dirnames[:] = []
                continue
            for name in filenames:
                src = here / name
                try:
                    size = src.stat().st_size
                except OSError:
                    continue
                plan.append((src, rel_root / src.relative_to(entry.path), size))
    return plan


def copy_size(entries: list[OutputEntry], include_images: bool) -> int:
    return sum(e.size - (0 if include_images else e.images_size) for e in entries)


def copy_outputs(entries: list[OutputEntry], output_dir: Path, destination: Path, include_images: bool = False,
                 progress: Callable[[int, int, str], None] | None = None,
                 cancel: threading.Event | None = None, pause: threading.Event | None = None) -> CopyResult:
    """Copy the entries to <destination>/VSBC_outputs/, keeping the tests/<date>/<test> layout.

    Checks the free space first and verifies every file's size. `pause` (set =
    paused) lets a recording run without competing for the disk.
    """
    plan = _copy_plan(entries, output_dir, include_images)
    needed = sum(size for _, _, size in plan)
    target = Path(destination) / COPY_FOLDER
    free = shutil.disk_usage(destination).free
    if needed > free - 50 * 1024 ** 2:
        raise OSError(f"Not enough space on {destination}: the copy needs {human_size(needed)}, "
                      f"{human_size(free)} is free.")
    result = CopyResult(target)
    done = 0
    for src, rel, size in plan:
        while pause is not None and pause.is_set() and not (cancel and cancel.is_set()):
            time.sleep(0.2)
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        dst = target / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if dst.stat().st_size != size:
                result.errors.append(f"{rel}: size differs after copying")
        except OSError as e:
            result.errors.append(f"{rel}: {e}")
            if getattr(e, "errno", None) == 28:     # ENOSPC: nothing more will fit
                break
            continue
        done += size
        result.files += 1
        result.bytes = done
        if progress is not None:
            progress(done, needed, str(rel))
    if hasattr(os, "sync"):
        os.sync()
    return result


# ------------------------------------------------------------------ deleting --

def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def delete_images(entry: OutputEntry) -> int:
    """Delete a recording's raw camera images; keeps data, video, DIC results and plots. Returns bytes freed."""
    if not entry.images_dir.is_dir():
        return 0
    freed, count = entry.images_size or _scan(entry.images_dir)[0], entry.n_images
    shutil.rmtree(entry.images_dir)
    (entry.camera_dir / IMAGES_DELETED_FILE).write_text(
        f"{count} camera images ({human_size(freed)}) were deleted on {datetime.now():%Y-%m-%d %H:%M} "
        "to free space. frames.csv still lists them.\n", encoding="utf-8")
    with _cache_lock:
        _size_cache.pop(str(entry.path), None)
    return freed


def delete_entry(entry: OutputEntry, output_dir: Path) -> int:
    """Delete a whole test or recording folder. Returns bytes freed."""
    if not _inside(entry.path, output_dir) or Path(entry.path).resolve() == Path(output_dir).resolve():
        raise ValueError(f"{entry.path} is not inside {output_dir}")
    freed = entry.size or _scan(entry.path)[0]
    shutil.rmtree(entry.path)
    day = entry.path.parent
    try:
        day.rmdir()                   # only if that date has no other tests
    except OSError:
        pass
    with _cache_lock:
        _size_cache.pop(str(entry.path), None)
    return freed


def delete_old_logs(logs_dir: Path, days: float = 30.0) -> tuple[int, int]:
    """Delete log files not changed for `days` (the current app log is kept). Returns (files, bytes)."""
    cutoff = time.time() - days * 86400
    count = freed = 0
    if not Path(logs_dir).is_dir():
        return 0, 0
    for path in Path(logs_dir).iterdir():
        if not path.is_file() or path.name == "vsbc.log" or ".log" not in path.name:
            continue
        try:
            st = path.stat()
            if st.st_mtime < cutoff:
                path.unlink()
                count += 1
                freed += st.st_size
        except OSError:
            continue
    return count, freed


# ------------------------------------------------------------- USB drives --

@dataclass(frozen=True)
class Drive:
    path: Path
    label: str
    free: int
    total: int

    def text(self) -> str:
        return f"{self.label} ({self.path}) — {human_size(self.free)} free"


def removable_drives() -> list[Drive]:
    """USB sticks and other removable drives that are mounted."""
    paths: list[tuple[Path, str]] = []
    if sys.platform.startswith("win"):
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if kernel32.GetDriveTypeW(root) != 2:          # DRIVE_REMOVABLE
                continue
            name = ctypes.create_unicode_buffer(261)
            kernel32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0)
            paths.append((Path(root), name.value or root))
    elif sys.platform == "darwin":
        for p in Path("/Volumes").glob("*"):
            if p.is_dir() and os.path.ismount(p) and p.resolve() != Path("/"):
                paths.append((p, p.name))
    else:
        user = getpass.getuser()
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/media")):
            if not base.is_dir():
                continue
            for p in base.iterdir():
                if p.is_dir() and os.path.ismount(p) and all(p != q for q, _ in paths):
                    paths.append((p, p.name))
    drives = []
    for p, label in paths:
        try:
            usage = shutil.disk_usage(p)
        except OSError:
            continue
        drives.append(Drive(p, label, usage.free, usage.total))
    return drives


def _device_of(mount_point: Path) -> str | None:
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[1].replace("\\040", " ") == str(mount_point):
                return parts[0]
    except OSError:
        pass
    return None


def eject(mount_point: Path) -> str:
    """Flush and unmount a drive so it can be unplugged. Returns a message for the operator."""
    if hasattr(os, "sync"):
        os.sync()
    if sys.platform.startswith("linux"):
        commands = []
        if shutil.which("gio"):
            commands.append(["gio", "mount", "-u", str(mount_point)])
        device = _device_of(Path(mount_point))
        if device and shutil.which("udisksctl"):
            commands.append(["udisksctl", "unmount", "-b", device])
        for cmd in commands:
            try:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.warning("eject %s: %s", cmd, e)
                continue
            if done.returncode == 0:
                return f"{mount_point} can be unplugged now."
            log.warning("eject %s: %s", cmd, (done.stderr or done.stdout).strip())
        return (f"Could not unmount {mount_point}; the files are flushed. "
                "Use the desktop's eject button before unplugging it.")
    return "The files are flushed. Use the system's Eject / Safely Remove before unplugging the drive."
