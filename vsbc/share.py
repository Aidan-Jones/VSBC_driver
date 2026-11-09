"""One zip per test for uploading to the cloud: Outputs/upload/<date>/<test>.zip.

A test folder holds thousands of files (one camera frame per load sample, the
full-rate frames at the break, DIC results...). The zip holds everything a
person needs to look at the test on another computer, in one file:

* every file of the test (data.csv, summary, plot.png, video.mp4, meta.json,
  the camera's frames.csv, DIC results, plots, crack-tip CSVs...);
* of the raw camera images, only frame 0 (the DIC reference) and the frames of
  the full-rate window(s) (the break). The routine 1:1 frames stay on this
  computer (copy them with the Outputs tab's USB copy if needed).

Standard library only: runs in the GUI and in the camera environment.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path

UPLOAD_DIR = "upload"
STORED = {".mp4", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".npz", ".gz"}   # already compressed
SKIP = {"postprocess_status.json"}
FRAME_RE = re.compile(r"^frame_(\d+)\.tiff?$", re.IGNORECASE)


def upload_dir(output_dir: Path) -> Path:
    return Path(output_dir) / UPLOAD_DIR


def upload_path(folder: Path, output_dir: Path) -> Path:
    """Outputs/upload/<date>/<name>.zip for Outputs/tests/<date>/<name> (or Outputs/camera/<date>/<name>)."""
    folder = Path(folder)
    return upload_dir(output_dir) / folder.parent.name / f"{folder.name}.zip"


def _camera_dir(folder: Path) -> Path:
    return folder / "camera" if (folder / "camera").is_dir() else folder


def _windows(camera_dir: Path) -> list[tuple[int, int]]:
    try:
        settings = json.loads((camera_dir / "capture_settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(settings, dict):
        return []
    windows = settings.get("full_rate_windows") or settings.get("crack_windows") or []
    out = []
    for w in windows:
        try:
            out.append((int(w[0]), int(w[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _keep_image(name: str, windows: list[tuple[int, int]]) -> bool:
    m = FRAME_RE.match(name)
    if m is None:
        return True                       # not a frame: keep it
    idx = int(m.group(1))
    return idx == 0 or any(start <= idx <= end for start, end in windows)


def upload_files(folder: Path) -> list[tuple[Path, str]]:
    """(file, name inside the zip) for everything that goes into a test's upload zip."""
    folder = Path(folder)
    images = (_camera_dir(folder) / "images").resolve()
    windows = _windows(_camera_dir(folder))
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        here = Path(dirpath)
        in_images = here.resolve() == images
        for name in sorted(filenames):
            if name in SKIP or name.endswith(".part"):
                continue
            if in_images and not _keep_image(name, windows):
                continue
            path = here / name
            files.append((path, (Path(folder.name) / path.relative_to(folder)).as_posix()))
    return files


def build_upload_zip(folder: Path, output_dir: Path) -> Path:
    """Write (or rewrite) the test's upload zip; returns its path."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} does not exist")
    target = upload_path(folder, output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", allowZip64=True) as zf:
            for path, arcname in upload_files(folder):
                stored = path.suffix.lower() in STORED
                try:
                    zf.write(path, arcname, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
                             compresslevel=None if stored else 6)
                except FileNotFoundError:
                    continue              # deleted while zipping
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target
