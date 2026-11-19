"""JSON files the app writes (meta.json, calibration records, sync.json...).

Standard JSON has no NaN or Infinity. Python's json module writes them anyway
(and never passes floats to `default=`), which breaks strict readers such as
JavaScript or spreadsheet imports. Everything here writes them as null.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path


def clean_json(obj):
    """obj with every non-finite float (also NumPy scalars) replaced by None, recursively."""
    if hasattr(obj, "item") and callable(obj.item) and getattr(obj, "shape", None) == ():
        obj = obj.item()                      # NumPy scalar -> Python int / float / bool
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    return obj


def _default(obj):
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        return clean_json(asdict(obj))
    if hasattr(obj, "tolist"):                # NumPy arrays
        return clean_json(obj.tolist())
    return str(obj)                           # Path and anything else


def dumps(data, indent: int | None = 2) -> str:
    """JSON text with NaN / Infinity written as null; Paths and other objects as strings."""
    return json.dumps(clean_json(data), indent=indent, default=_default, allow_nan=False)


def write_json(path: Path, data) -> None:
    """Write `data` to `path` atomically (a reader never sees half a file)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(data), encoding="utf-8")
    os.replace(tmp, path)
