# -*- coding: utf-8 -*-
"""
crack_tip_viewer.py

Step through a run frame by frame and look at the crack tip.

Shows each frame that has DIC results (whole-test overview + every frame of
the full-rate crack window), with:
    - strain overlay (eps_11 = vertical strain), toggle with 's'
    - image-tracker tip  (red +)    from crack_tip_image.csv, if it exists
    - opening-method tip (cyan x)   from crack_tip_opening.csv, if it exists
    - YOUR tip marks     (green o)  click to mark; saved to
      <run>/crack_tip_manual.csv (crack_tip_image.py can use it as MANUAL_CSV)

The strain overlay is drawn at the reference-image positions of the strain
points (not moved by the displacement), so near a wide-open crack it is off
by the opening.

Controls (click the window first):
    d / a         next / previous frame          (D / A = 10 frames)
    c             jump to the start of the crack window
    s             strain overlay on/off
    z             2x zoom around the last tip (or the notch) on/off
    left click    mark the crack tip on this frame
    right click   delete your mark on this frame
    r             toggle raw mode: ALL saved frames (no strain overlay)
    q / Esc       quit (your marks are saved automatically)
    slider        jump anywhere

Use:
    python3 crack_tip_viewer.py                      -> newest run
    python3 crack_tip_viewer.py ~/vsbc_captures/<run folder>
(for a vsbc GUI test, the run folder is <test folder>/camera)
"""

import os
import re
import sys
import glob
import collections

import numpy as np
import cv2

from view_frames import read_frames_csv, frame_times

RUNS_ROOT = (r"C:\VSBC_DIC_Test\captures" if os.name == "nt"
             else os.path.expanduser("~/vsbc_captures"))
DISPLAY_WIDTH = 1500
STRAIN_COLUMN = "eps_11"
STRAIN_CLIM = None          # e.g. (-0.002, 0.01); None = 2nd-98th percentile of
                            # the crack window
OVERLAY_ALPHA = 0.45


def _num(path):
    m = re.search(r"frame[_\-]?(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else -1


def _read_tips(path, xcol, ycol=None):
    if not os.path.exists(path):
        return {}
    a = np.genfromtxt(path, delimiter=",", names=True, dtype=None, encoding=None)
    if a.size == 0:
        return {}
    a = np.atleast_1d(a)
    out = {}
    for r in a:
        x = float(r[xcol])
        if np.isfinite(x):
            y = float(r[ycol]) if ycol and ycol in a.dtype.names else np.nan
            out[int(r["frame_index"])] = (x, y)
    return out


class CrackViewer:
    def __init__(self, run_folder):
        self.run = run_folder
        self.img_dir = os.path.join(run_folder, "images")
        self.res_dir = os.path.join(run_folder, "pyvale_results")
        self.all_paths = {_num(p): p for p in glob.glob(os.path.join(self.img_dir, "frame_*.tif"))}
        if not self.all_paths:
            raise SystemExit(f"No frames in {self.img_dir}")

        # strain files per frame
        self.strain_files = {}
        for f in glob.glob(os.path.join(self.res_dir, "strain_*.csv")):
            self.strain_files[_num(f)] = f
        dic_frames = sorted(set(self.strain_files) |
                            {_num(f) for f in glob.glob(os.path.join(self.res_dir, "dic_results_*"))})
        self.dic_frames = [n for n in dic_frames if n in self.all_paths]
        self.raw_frames = sorted(self.all_paths)
        self.raw = not self.dic_frames
        if self.raw:
            print("No DIC results yet: showing all saved frames without strain.")

        # time + crack marker
        rows = read_frames_csv(run_folder)
        self.t_of = frame_times(rows)
        marks = [int(r["frame_index"]) for r in rows
                 if (r.get("marker") or "").startswith("crack")]
        self.crack_mark = marks[0] if marks else None

        # tracker results + manual marks
        self.tip_img = _read_tips(os.path.join(run_folder, "crack_tip_image.csv"),
                                  "crack_tip_x_px", "crack_tip_y_px")
        self.tip_open = _read_tips(os.path.join(run_folder, "crack_tip_opening.csv"),
                                   "tip_x_raw_px")
        self.open_y = None
        oc = os.path.join(run_folder, "crack_tip_opening.csv")
        if os.path.exists(oc):
            a = np.atleast_1d(np.genfromtxt(oc, delimiter=",", names=True))
            if a.size and "crack_y_px" in a.dtype.names:
                self.open_y = float(a["crack_y_px"][0])
        self.manual_path = os.path.join(run_folder, "crack_tip_manual.csv")
        self.manual = _read_tips(self.manual_path, "crack_tip_x_px", "crack_tip_y_px")

        # strain columns + color range
        self.cols = None
        self.clim = STRAIN_CLIM
        self._strain_cache = collections.OrderedDict()
        self._img_cache = collections.OrderedDict()
        if self.strain_files:
            with open(next(iter(self.strain_files.values()))) as fh:
                self.cols = [c.strip().strip('"').strip("'") for c in fh.readline().split(",")]
            if self.clim is None:
                sample = self._window(self.frames())[::max(1, len(self._window(self.frames())) // 40)]
                vals = [self._strain_points(n)[:, 2] for n in sample if n in self.strain_files]
                vals = np.concatenate(vals) if vals else np.array([0.0, 1e-3])
                self.clim = (float(np.nanpercentile(vals, 2)), float(np.nanpercentile(vals, 98)))
            print(f"Strain overlay: {STRAIN_COLUMN}, color range "
                  f"{self.clim[0]:.5f} to {self.clim[1]:.5f}")

        self.i = self._window(self.frames())[0]
        self.i = self.frames().index(self.i)
        self.show_strain = bool(self.strain_files)
        self.zoom = False
        self.scale = 1.0
        self.offset = (0, 0)

    # -- frame lists ---------------------------------------------------------
    def frames(self):
        return self.raw_frames if self.raw else self.dic_frames

    @staticmethod
    def _window(fr):
        """Frames of the full-rate window (consecutive numbers); else all."""
        fr = list(fr)
        if len(fr) < 2:
            return fr
        d = np.diff(fr) == 1
        c = np.zeros(len(fr), bool)
        c[:-1] |= d
        c[1:] |= d
        return [f for f, k in zip(fr, c) if k] or fr

    # -- data ----------------------------------------------------------------
    def _img(self, n):
        if n in self._img_cache:
            self._img_cache.move_to_end(n)
            return self._img_cache[n]
        im = cv2.imread(self.all_paths[n], cv2.IMREAD_GRAYSCALE)
        self._img_cache[n] = im
        if len(self._img_cache) > 100:
            self._img_cache.popitem(last=False)
        return im

    def _strain_points(self, n):
        if n in self._strain_cache:
            return self._strain_cache[n]
        a = np.loadtxt(self.strain_files[n], delimiter=",", skiprows=1, ndmin=2)
        j = self.cols.index(STRAIN_COLUMN)
        pts = a[:, [0, 1, j]]
        self._strain_cache[n] = pts
        if len(self._strain_cache) > 100:
            self._strain_cache.popitem(last=False)
        return pts

    # -- drawing -------------------------------------------------------------
    def _strain_overlay(self, n, shape):
        pts = self._strain_points(n)
        xs, ys = np.unique(pts[:, 0]), np.unique(pts[:, 1])
        if xs.size < 2 or ys.size < 2:
            return None, None
        grid = np.full((ys.size, xs.size), np.nan)
        grid[np.searchsorted(ys, pts[:, 1]), np.searchsorted(xs, pts[:, 0])] = pts[:, 2]
        lo, hi = self.clim
        norm = np.clip((grid - lo) / max(hi - lo, 1e-12), 0, 1)
        col = cv2.applyColorMap((np.nan_to_num(norm) * 255).astype(np.uint8), cv2.COLORMAP_JET)
        valid = np.isfinite(grid).astype(np.uint8)
        dx, dy = xs[1] - xs[0], ys[1] - ys[0]
        x0, y0 = int(round(xs[0] - dx / 2)), int(round(ys[0] - dy / 2))
        w, h = int(round(dx * xs.size)), int(round(dy * ys.size))
        col = cv2.resize(col, (w, h), interpolation=cv2.INTER_NEAREST)
        valid = cv2.resize(valid, (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)
        full = np.zeros(shape + (3,), np.uint8)
        mask = np.zeros(shape, bool)
        ya, yb = max(0, y0), min(shape[0], y0 + h)
        xa, xb = max(0, x0), min(shape[1], x0 + w)
        full[ya:yb, xa:xb] = col[ya - y0:yb - y0, xa - x0:xb - x0]
        mask[ya:yb, xa:xb] = valid[ya - y0:yb - y0, xa - x0:xb - x0]
        return full, mask

    def _center(self, n):
        for src in (self.manual, self.tip_img, self.tip_open):
            if n in src:
                x, y = src[n]
                return x, (y if np.isfinite(y) else (self.open_y or None))
        for src in (self.tip_img, self.manual):
            if src:
                k = min(src)
                return src[k]
        return None

    def render(self):
        n = self.frames()[self.i]
        img = self._img(n)
        disp = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if self.show_strain and not self.raw and n in self.strain_files:
            ov, mask = self._strain_overlay(n, img.shape)
            if ov is not None:
                blend = cv2.addWeighted(disp, 1 - OVERLAY_ALPHA, ov, OVERLAY_ALPHA, 0)
                disp[mask] = blend[mask]

        def mark(src, n, color, kind):
            if n not in src:
                return
            x, y = src[n]
            if not np.isfinite(y):
                y = self.open_y if self.open_y is not None else img.shape[0] / 2
            p = (int(round(x)), int(round(y)))
            if kind == "+":
                cv2.drawMarker(disp, p, color, cv2.MARKER_CROSS, 26, 2)
            elif kind == "x":
                cv2.drawMarker(disp, p, color, cv2.MARKER_TILTED_CROSS, 22, 2)
            else:
                cv2.circle(disp, p, 9, color, 2)

        mark(self.tip_img, n, (0, 0, 255), "+")
        mark(self.tip_open, n, (255, 255, 0), "x")
        mark(self.manual, n, (0, 255, 0), "o")

        # zoom / scale
        H, W = img.shape
        if self.zoom:
            c = self._center(n) or (W / 2, H / 2)
            cx = int(c[0])
            cy = int(c[1]) if c[1] is not None and np.isfinite(c[1]) else H // 2
            hw, hh = int(DISPLAY_WIDTH / 4), int(min(H, DISPLAY_WIDTH * 0.25) / 2)
            x0 = int(np.clip(cx - hw, 0, max(0, W - 2 * hw)))
            y0 = int(np.clip(cy - hh, 0, max(0, H - 2 * hh)))
            disp = disp[y0:y0 + 2 * hh, x0:x0 + 2 * hw]
            self.scale, self.offset = 2.0, (x0, y0)
        else:
            self.scale, self.offset = DISPLAY_WIDTH / W, (0, 0)
        disp = cv2.resize(disp, None, fx=self.scale, fy=self.scale,
                          interpolation=cv2.INTER_NEAREST if self.zoom else cv2.INTER_AREA)

        # text
        lines = []
        t = self.t_of.get(n)
        s = f"frame {n}  ({self.i + 1}/{len(self.frames())})"
        if t is not None:
            s += f"   t = {t:.4f} s"
            if self.crack_mark is not None and self.crack_mark in self.t_of:
                s += f"   {1000 * (t - self.t_of[self.crack_mark]):+.1f} ms from 'c'"
        s += "   [RAW]" if self.raw else ("   strain on" if self.show_strain else "")
        lines.append(s)
        tips = []
        for lab, src in (("image", self.tip_img), ("opening", self.tip_open), ("you", self.manual)):
            if n in src:
                tips.append(f"{lab} x={src[n][0]:.0f}")
        lines.append("tip: " + ("   ".join(tips) if tips else "-") +
                     "   (left click = mark, right click = delete)")
        bar = np.zeros((52, disp.shape[1], 3), np.uint8)
        for k, txt in enumerate(lines):
            cv2.putText(bar, txt, (8, 20 + 22 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 255, 255), 1)
        return np.vstack([bar, disp])

    # -- interaction ---------------------------------------------------------
    def on_mouse(self, event, x, y, flags=None, param=None):
        n = self.frames()[self.i]
        y -= 52                                     # text bar
        if y < 0:
            return
        ix = x / self.scale + self.offset[0]
        iy = y / self.scale + self.offset[1]
        if event == cv2.EVENT_LBUTTONDOWN:
            self.manual[n] = (float(ix), float(iy))
            self.save_manual()
        elif event == cv2.EVENT_RBUTTONDOWN and n in self.manual:
            del self.manual[n]
            self.save_manual()

    def save_manual(self):
        with open(self.manual_path, "w") as fh:
            fh.write("frame_index,filename,crack_tip_x_px,crack_tip_y_px,time_s\n")
            for n in sorted(self.manual):
                x, y = self.manual[n]
                t = self.t_of.get(n, float("nan"))
                fh.write(f"{n},frame_{n:06d}.tif,{x:.1f},{y:.1f},{t:.6f}\n")

    def key(self, k):
        fr = self.frames()
        if k in (ord("q"), 27):
            return False
        if k == ord("d"):
            self.i = min(len(fr) - 1, self.i + 1)
        elif k == ord("a"):
            self.i = max(0, self.i - 1)
        elif k == ord("D"):
            self.i = min(len(fr) - 1, self.i + 10)
        elif k == ord("A"):
            self.i = max(0, self.i - 10)
        elif k == ord("c"):
            self.i = fr.index(self._window(fr)[0])
        elif k == ord("s"):
            self.show_strain = not self.show_strain
        elif k == ord("z"):
            self.zoom = not self.zoom
        elif k == ord("r") and self.dic_frames:
            n = fr[self.i]
            self.raw = not self.raw
            new = self.frames()
            self.i = int(np.argmin(np.abs(np.array(new) - n)))
        return True


def main(run_folder, show=True, script=None):
    v = CrackViewer(run_folder)
    print(f"{len(v.dic_frames)} frames with DIC, {len(v.raw_frames)} saved frames. "
          f"Manual marks: {v.manual_path}")
    win = "Crack tip viewer (a/d step, c crack, s strain, z zoom, click = mark tip, q quit)"
    if show:
        cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(win, v.on_mouse)
        state = {"jump": None, "n": len(v.frames())}
        cv2.createTrackbar("frame", win, v.i, max(1, len(v.frames()) - 1),
                           lambda val: state.__setitem__("jump", val))
    steps = list(script) if script is not None else None
    while True:
        if show and state["jump"] is not None:
            v.i = min(int(state["jump"]), len(v.frames()) - 1)
            state["jump"] = None
        frame = v.render()
        if show:
            cv2.imshow(win, frame)
            k = cv2.waitKey(30) & 0xFF
        else:
            if not steps:
                break
            st = steps.pop(0)
            if isinstance(st, tuple):          # simulated click (event, x, y)
                v.on_mouse(*st)
                continue
            k = ord(st)
        if k == 255:
            continue
        if not v.key(k):
            break
        if show:
            if state["n"] != len(v.frames()):
                state["n"] = len(v.frames())
                cv2.setTrackbarMax("frame", win, max(1, state["n"] - 1))
            cv2.setTrackbarPos("frame", win, v.i)
            state["jump"] = None
    if show:
        cv2.destroyAllWindows()
    v.save_manual()
    print(f"{len(v.manual)} manual tip marks saved to {v.manual_path}")
    return v


if __name__ == "__main__":
    if len(sys.argv) > 1:
        folder = sys.argv[1]
    else:
        runs = sorted(d for d in glob.glob(os.path.join(RUNS_ROOT, "*"))
                      if os.path.isdir(os.path.join(d, "images")))
        if not runs:
            sys.exit(f"No runs in {RUNS_ROOT}")
        folder = runs[-1]
    print(f"Run: {folder}")
    main(folder)
