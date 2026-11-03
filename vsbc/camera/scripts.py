"""The camera scripts folder: what the GUI needs from each script, a check
that an updated script still has it, and pointing a script's settings at a test.

The GUI never imports the scripts (they need cv2 and pyvale); it reads them
with `ast`. The camera process and the post-processing jobs, which run in the
camera environment, import and run them. Standard library only, so this
module works in both environments.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

CAPTURE = "capture_dic_live.py"
DIC_RUN = "pyvale_dic_run.py"
CRACK_IMAGE = "crack_tip_image.py"
CRACK_OPENING = "crack_tip_opening.py"
SCRIPTS = (CAPTURE, DIC_RUN, CRACK_IMAGE, CRACK_OPENING)

# Settings the GUI points at each test; the job runner rewrites them before running the script.
TEST_SETTINGS = {
    DIC_RUN: ("image_folder", "output_folder", "RUN_DIC", "MAKE_ANIMATION", "ANIM_STEP"),
    CRACK_IMAGE: ("RUN_FOLDER", "NOTCH_TIP", "CRACK_FROM_LEFT"),
    CRACK_OPENING: ("RUN_FOLDER", "NOTCH_TIP", "CRACK_FROM_LEFT", "subset_size"),
}

# capture_dic_live.py settings the GUI uses as defaults; one that is missing falls back to these.
CAPTURE_DEFAULTS = {
    "TEST_NAME": "test", "APERTURE_LABEL": "",
    "EXPOSURE_US": 1488.0, "FPS": None, "SAVE_EVERY": 9, "BUFFER_SECONDS": 2.0, "POST_SECONDS": 1.0,
    "MAX_SECONDS": 45.0, "SETTLE_SECONDS": 5.0,
    "CROP": {"width": 1920, "height": 400, "offset_x": 8, "offset_y": 408},
    "DIC_STEP": 27, "CRACK_DIC_STEP": 1, "subset_size": 41, "subset_step": 20, "max_displacement": 64,
    "DIC_THREADS": 2, "FILL_BATCH": 25, "N_WRITERS": 4, "PREVIEW_SCALE": 0.4,
}
# name -> required. configure_camera is optional: the camera process has the same setup built in.
CAPTURE_FUNCTIONS = {"dic_worker": True, "configure_camera": False}


@dataclass
class ScriptCheck:
    name: str
    path: Path
    found: bool
    problems: list[str] = field(default_factory=list)   # the GUI cannot use the script
    notes: list[str] = field(default_factory=list)      # it can, with built-in fallbacks

    @property
    def ok(self) -> bool:
        return self.found and not self.problems

    def text(self) -> str:
        if not self.found:
            return f"✗ {self.name}: missing"
        mark = "✓" if self.ok else "✗"
        details = self.problems + self.notes
        return f"{mark} {self.name}" + (f": {'; '.join(details)}" if details else "")


def top_level_assignments(tree: ast.Module) -> dict[str, ast.stmt]:
    """name -> the first top-level statement that assigns it (`name = ...`)."""
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names = [node.target.id]
        for name in names:
            found.setdefault(name, node)
    return found


def _functions(tree: ast.Module) -> set[str]:
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_main_unguarded(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "main":
                return True
    return False


def check_script(path: Path) -> ScriptCheck:
    path = Path(path)
    name = path.name
    if not path.is_file():
        return ScriptCheck(name, path, False, [f"{name} is missing"])
    check = ScriptCheck(name, path, True)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as e:
        check.problems.append(f"cannot be read: {e}")
        return check
    assigns = top_level_assignments(tree)
    if name == CAPTURE:
        functions = _functions(tree)
        for function, required in CAPTURE_FUNCTIONS.items():
            if function not in functions:
                if required:
                    check.problems.append(f"no top-level {function}() (needed for the DIC)")
                else:
                    check.notes.append(f"no {function}(): built-in camera setup used")
        if _calls_main_unguarded(tree):
            check.problems.append('main() runs on import: put it under if __name__ == "__main__"')
        missing = [k for k in CAPTURE_DEFAULTS if k not in assigns]
        if missing:
            check.notes.append(f"built-in values for {', '.join(missing)}")
    elif name in TEST_SETTINGS:
        missing = [k for k in TEST_SETTINGS[name] if k not in assigns]
        if missing:
            check.problems.append(f"no top-level {', '.join(missing)} = ... for the GUI to point at the test")
    return check


def check_scripts(camera_dir: Path) -> list[ScriptCheck]:
    return [check_script(Path(camera_dir) / name) for name in SCRIPTS]


def load_module(camera_dir: Path, name: str = CAPTURE):
    """Import a script from the camera folder as a module (its main() does not run).

    Camera environment only: the scripts import cv2 (and pyvale inside functions).
    """
    import importlib.util
    import sys

    camera_dir = Path(camera_dir)
    if str(camera_dir) not in sys.path:
        sys.path.insert(0, str(camera_dir))
    stem = Path(name).stem
    spec = importlib.util.spec_from_file_location(stem, camera_dir / name)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {camera_dir / name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[stem] = module
    spec.loader.exec_module(module)
    return module


def _literal(node: ast.expr):
    """ast.literal_eval, plus dict(key=literal, ...) calls (CROP = dict(width=1920, ...))."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
        return {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
    return ast.literal_eval(node)


def literal_settings(camera_dir: Path) -> dict:
    """capture_dic_live.py's USER SETTINGS read without importing it (so it works in the app's
    environment): literal values; settings computed from expressions keep the built-in value."""
    out = dict(CAPTURE_DEFAULTS)
    try:
        tree = ast.parse((Path(camera_dir) / CAPTURE).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return out
    for name, node in top_level_assignments(tree).items():
        if name in out and getattr(node, "value", None) is not None:
            try:
                out[name] = _literal(node.value)
            except (ValueError, TypeError, SyntaxError):
                pass
    return out


def capture_settings(module) -> dict:
    """capture_dic_live.py's USER SETTINGS the GUI uses, with the built-in value for any it lacks."""
    out = {}
    for key, default in CAPTURE_DEFAULTS.items():
        value = getattr(module, key, default) if module is not None else default
        if isinstance(value, (bool, int, float, str, dict, type(None))):
            out[key] = value
        else:
            out[key] = default
    return out


def override_settings(source: str, overrides: dict) -> str:
    """The script with `NAME = <value>` inserted right after each overridden top-level setting.

    The original line stays (so `a = b = 1` keeps `b`); the new one wins for
    the rest of the script. Values must be literals (str, numbers, bool,
    None, tuples, lists, dicts). Raises KeyError for a setting the script
    does not have.
    """
    tree = ast.parse(source)
    assigns = top_level_assignments(tree)
    missing = [k for k in overrides if k not in assigns]
    if missing:
        raise KeyError(f"the script has no top-level {', '.join(missing)}")
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    # From the bottom up, so earlier line numbers stay valid.
    for key in sorted(overrides, key=lambda k: assigns[k].end_lineno, reverse=True):
        value = overrides[key]
        ast.literal_eval(repr(value))      # only literals can be written back as source
        lines.insert(assigns[key].end_lineno, f"{key} = {value!r}  # set by the vsbc GUI for this test\n")
    return "".join(lines)
