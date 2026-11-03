"""The DIC region of interest, the seed point and the notch tip.

Drawn in the Camera tab and stored as roi.npz with plain arrays (no pickle,
so the GUI's NumPy 1 and the camera environment's NumPy 2 both read it):

    mask             bool (height, width), True inside the ROI (pyvale's roi_mask)
    seed             int [x, y] (pyvale's seed: column, row)
    notch            int [x, y], or [-1, -1] when not set (the crack trackers' NOTCH_TIP)
    crack_from_left  bool
    image_shape      int [height, width]
    offsets          int [offset_x, offset_y] of the camera crop it was drawn on

Outputs/camera/last_roi.npz is the current one; each recording gets a copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Roi:
    mask: np.ndarray
    seed: tuple[int, int] | None = None
    notch: tuple[int, int] | None = None
    crack_from_left: bool = True
    offsets: tuple[int, int] = (-1, -1)

    @property
    def shape(self) -> tuple[int, int]:
        return tuple(self.mask.shape)

    def problems(self) -> list[str]:
        """What stops DIC from running with this ROI."""
        out = []
        if not self.mask.any():
            out.append("the ROI is empty")
        if self.seed is None:
            out.append("no seed point")
        elif not (0 <= self.seed[1] < self.mask.shape[0] and 0 <= self.seed[0] < self.mask.shape[1]) \
                or not self.mask[self.seed[1], self.seed[0]]:
            out.append("the seed point is outside the ROI")
        return out

    def matches(self, height: int, width: int, offsets=None) -> bool:
        if self.shape != (int(height), int(width)):
            return False
        return offsets is None or -1 in self.offsets or tuple(int(v) for v in offsets) == tuple(self.offsets)


def save_roi(path: Path, roi: Roi) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        np.savez(f, mask=roi.mask.astype(bool),
                 seed=np.array(roi.seed if roi.seed else (-1, -1), dtype=np.int64),
                 notch=np.array(roi.notch if roi.notch else (-1, -1), dtype=np.int64),
                 crack_from_left=np.array(bool(roi.crack_from_left)),
                 image_shape=np.array(roi.mask.shape, dtype=np.int64),
                 offsets=np.array(roi.offsets, dtype=np.int64))


def load_roi(path: Path) -> Roi | None:
    try:
        with np.load(Path(path), allow_pickle=False) as data:
            mask = data["mask"].astype(bool)
            seed = tuple(int(v) for v in data["seed"]) if "seed" in data else (-1, -1)
            notch = tuple(int(v) for v in data["notch"]) if "notch" in data else (-1, -1)
            left = bool(data["crack_from_left"]) if "crack_from_left" in data else True
            offsets = tuple(int(v) for v in data["offsets"]) if "offsets" in data else (-1, -1)
    except (OSError, ValueError, KeyError):
        return None
    return Roi(mask, None if -1 in seed else seed, None if -1 in notch else notch, left, offsets)


def rect_mask(shape, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    xa, xb = sorted((int(round(x0)), int(round(x1))))
    ya, yb = sorted((int(round(y0)), int(round(y1))))
    mask[max(ya, 0):min(yb, h), max(xa, 0):min(xb, w)] = True
    return mask


def polygon_mask(shape, points) -> np.ndarray:
    """Pixels whose centres lie inside the polygon (even-odd rule)."""
    h, w = shape
    pts = np.asarray(points, dtype=float)
    mask = np.zeros((h, w), dtype=bool)
    if len(pts) < 3:
        return mask
    x0, y0 = np.floor(pts.min(axis=0)).astype(int)
    x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1 + 1, w), min(y1 + 1, h)
    if x0 >= x1 or y0 >= y1:
        return mask
    ys, xs = np.mgrid[y0:y1, x0:x1]
    px, py = xs + 0.5, ys + 0.5
    inside = np.zeros(px.shape, dtype=bool)
    for (ax, ay), (bx, by) in zip(pts, np.roll(pts, -1, axis=0)):
        crosses = (ay > py) != (by > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = ax + (py - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (px < x_at)
    mask[y0:y1, x0:x1] = inside
    return mask


def outline(mask: np.ndarray, step: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """x, y of the mask's boundary pixels (for drawing it over the image)."""
    m = mask[::step, ::step]
    edge = m & ~(np.roll(m, 1, 0) & np.roll(m, -1, 0) & np.roll(m, 1, 1) & np.roll(m, -1, 1))
    edge[[0, -1], :] &= m[[0, -1], :]
    edge[:, [0, -1]] &= m[:, [0, -1]]
    ys, xs = np.nonzero(edge)
    return xs * step, ys * step
