"""Build VSBC_Manual.pdf (repository root) from docs/manual/manual.html.

    python docs/manual/build_manual.py                   # new screenshots, then the PDF
    python docs/manual/build_manual.py --no-screenshots  # the PDF only, from the PNGs in docs/manual/img/

1. The screenshots (capture_screenshots.py) build the real GUI offscreen against the simulator, so they
   need the app's GUI dependencies. Use any environment that runs `vsbc gui --sim`, or a throwaway one:

       py -3.12 -m venv %TEMP%\\vsbc-manual                      (macOS / Linux: python3 -m venv /tmp/vsbc-manual)
       %TEMP%\\vsbc-manual\\Scripts\\pip install pyserial numpy matplotlib pyqtgraph PyQt5
       %TEMP%\\vsbc-manual\\Scripts\\python docs/manual/build_manual.py

   The script finds the vsbc package in this repository by itself; it does not need to be installed.
2. The PDF is printed by headless Microsoft Edge or Google Chrome (Chromium 131 or newer, for the page
   numbers). It is found in the usual places on Windows, macOS and Linux; --browser sets the path.

Edit manual.html for the text; the page layout (US Letter, numbering, page breaks) is its print CSS.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HTML = HERE / "manual.html"
PDF = REPO / "VSBC_Manual.pdf"


def find_browser() -> str | None:
    names = ["msedge", "microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
             "chromium", "chromium-browser", "chrome"]
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [Path(base, "Microsoft/Edge/Application/msedge.exe"),
                               Path(base, "Google/Chrome/Application/chrome.exe")]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                       Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                       Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def print_pdf(browser: str, html: Path = HTML, pdf: Path = PDF) -> None:
    profile = tempfile.mkdtemp(prefix="vsbc_manual_browser_")    # never touches a running browser's profile
    tmp_pdf = Path(profile) / "manual.pdf"
    try:
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={profile}", "--no-pdf-header-footer", "--virtual-time-budget=10000",
               f"--print-to-pdf={tmp_pdf}", html.as_uri()]
        subprocess.run(cmd, check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # On Windows msedge.exe can hand the work to another process and return before the PDF is
        # written: wait until the file is there and has stopped growing.
        deadline, last = time.monotonic() + 180, -1
        while time.monotonic() < deadline:
            size = tmp_pdf.stat().st_size if tmp_pdf.is_file() else -1
            if size > 10_000 and size == last:
                break
            last = size
            time.sleep(1.0)
        else:
            raise RuntimeError(f"{Path(browser).name} did not write the PDF")
        shutil.copyfile(tmp_pdf, pdf)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-screenshots", action="store_true", help="keep the PNGs in docs/manual/img/")
    ap.add_argument("--browser", help="path to msedge / chrome (default: found automatically)")
    args = ap.parse_args()

    if not args.no_screenshots:
        print("Capturing screenshots (offscreen, simulator)...")
        # In its own process: Qt's offscreen platform must be chosen before Qt loads.
        subprocess.run([sys.executable, str(HERE / "capture_screenshots.py")], check=True)

    browser = args.browser or find_browser()
    if not browser:
        print("No Microsoft Edge or Google Chrome found: pass --browser <path>.", file=sys.stderr)
        return 1
    print(f"Printing {HTML.name} with {Path(browser).name}...")
    print_pdf(browser)
    print(f"Wrote {PDF} ({PDF.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
