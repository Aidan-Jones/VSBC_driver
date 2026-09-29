"""Application log: Outputs/logs/vsbc.log (rotating) plus the console."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO, console: bool = True) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "vsbc.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_vsbc", False):
            root.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    file_handler._vsbc = True
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        stream.setLevel(logging.WARNING)
        stream._vsbc = True
        root.addHandler(stream)
    return path
