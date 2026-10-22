"""Command line entry point: `vsbc <command>` or `python -m vsbc <command>`.

    vsbc                      touchscreen GUI (same as `vsbc gui`)
    vsbc terminal             serial terminal + session log (replaces serial_logger.py)
    vsbc test --specimen A1   pull-to-failure test without the GUI
    vsbc calibrate load       load-cell calibration with known weights
    vsbc calibrate load --no-mega   the same on the Pi alone, no Mega attached
    vsbc calibrate travel     steps/mm calibration with a measured move
    vsbc analyze <folder>     recompute a test's summary and plot
    vsbc flash                compile + upload the firmware (arduino-cli)
    vsbc ports                list serial ports
    vsbc loadcell             check the HX711 wired to the Pi's GPIO pins

Add --sim to any hardware command to use the built-in simulator.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from vsbc import __version__
from vsbc.config import LOADCELL_SOURCES, Config, load_config
from vsbc.firmware import FirmwareToolError
from vsbc.link import DeviceError, LinkClosed, PortNotFound, find_port, list_ports
from vsbc.logs import setup_logging
from vsbc.machine import FirmwareMismatch, Machine
from vsbc.procedures import PreflightError

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers --

def confirm(question: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def ask_float(question: str, default: float | None = None) -> float:
    while True:
        hint = f" [{default:g}]" if default is not None else ""
        text = input(f"{question}{hint}: ").strip()
        if not text and default is not None:
            return default
        try:
            return float(text)
        except ValueError:
            print("  Please enter a number.")


def _connect(args, cfg: Config, **kwargs) -> Machine:
    m = Machine(cfg)
    m.connect(getattr(args, "port", None), sim=getattr(args, "sim", False), **kwargs)
    if m.sim is not None and not m.status().referenced:
        m.zero()   # the simulator starts unreferenced, like a freshly reset Mega; its zero is as good as any
        m.warnings = [w for w in m.warnings if "referenced" not in w]
    for warning in m.warnings:
        print(f"Warning: {warning}")
    return m


# ----------------------------------------------------------------- commands --

def cmd_gui(args, cfg: Config) -> int:
    try:
        from vsbc.gui.app import run_gui
    except ImportError as e:
        print(f"The GUI needs PyQt5 and pyqtgraph ({e}).\n"
              "  Pi:      sudo apt install python3-pyqt5 python3-pyqtgraph\n"
              "  Windows: pip install PyQt5 pyqtgraph", file=sys.stderr)
        return 1
    return run_gui(cfg, port=getattr(args, "port", None), sim=getattr(args, "sim", False),
                   fullscreen=getattr(args, "fullscreen", False) or cfg.gui.fullscreen)


def cmd_ports(args, cfg: Config) -> int:
    ports = list_ports()
    if not ports:
        print("No serial ports found.")
    for device, description in ports:
        print(f"{device:15s} {description}")
    return 0


def cmd_terminal(args, cfg: Config) -> int:
    m = Machine(cfg)
    m.add_raw_listener(lambda text, sent: None if sent else print(text, flush=True))
    info = m.connect(args.port, sim=args.sim, stream=False, watchdog=False)
    print(f"Connected to {m.port}: {info.name} {info.version}. Session log in {cfg.logs_dir}")
    print('Type commands and press Enter (? for help). Any command stops a running move. '
          'Ctrl+C or "quit" to exit.')
    try:
        while True:
            line = input().lstrip("﻿").strip()   # a byte-order mark can arrive with piped input
            if line.lower() in ("quit", "exit"):
                break
            if line:
                m.send_raw(line)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        m.disconnect()   # sends STOP first
    return 0


def cmd_test(args, cfg: Config) -> int:
    from vsbc.procedures import PullTestRunner, PullTestSpec

    overrides = {
        "rate_mm_min": args.rate, "max_extension_mm": args.max_ext, "stop_load_n": args.stop_load,
        "break_drop_pct": args.break_drop, "break_min_n": args.break_min,
        "area_mm2": args.area, "gauge_length_mm": args.gauge,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if args.no_tare:
        overrides["tare_before"] = False
    if args.return_after:
        overrides["return_after"] = True
    spec = PullTestSpec.from_defaults(args.specimen, cfg.test, operator=args.operator, notes=args.notes, **overrides)

    m = _connect(args, cfg)
    try:
        last = [0.0]

        def on_sample(sample, ext):
            now = time.monotonic()
            if now - last[0] >= 0.5:
                last[0] = now
                print(f"  t={sample.t_ms / 1000:8.2f} s   ext={ext:8.3f} mm   load={sample.load_n:9.2f} N", flush=True)

        runner = PullTestRunner(m, spec, cfg, on_sample=on_sample)
        warnings = runner.preflight()
        for warning in warnings:
            print(f"Warning: {warning}")
        if warnings and not args.yes and not confirm("Start anyway?"):
            return 1
        print(f"Running {spec.specimen_id}: {spec.rate_mm_min:g} mm/min, up to {spec.max_extension_mm:g} mm. "
              "Ctrl+C stops the test.")
        box = {}
        worker = threading.Thread(target=lambda: box.setdefault("result", runner.run()), daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                worker.join(0.2)
        except KeyboardInterrupt:
            print("\nStopping...")
            runner.request_stop()
            worker.join()
        result = box.get("result")
        if result is None:
            print("The test did not produce a result (see Outputs/logs/vsbc.log).", file=sys.stderr)
            return 1
        print()
        if result.summary:
            print(result.summary.to_text())
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)
        print(f"\nFiles: {result.folder}")
        return 1 if result.aborted else 0
    finally:
        m.disconnect()


def _collect_load_points(cfg: Config, collect, sim=None) -> list:
    """The zero point, then one point per known load the operator enters.

    `collect()` returns (mean, std, n) of the raw counts over a few seconds.
    """
    from vsbc.calibration import LoadPoint, parse_load

    mean, std, n = collect()
    points = [LoadPoint(0.0, mean, std, n, "zero")]
    idle_noise = std
    print(f"  0 N: {mean:.0f} counts (std {std:.1f}, {n} samples)")
    while True:
        text = input("Known load ('500 g', '2 kg', '10 N'), or Enter to finish: ").strip()
        if not text:
            break
        try:
            force = parse_load(text, cfg.loadcell.gravity)
        except ValueError as e:
            print(f"  {e}")
            continue
        if sim:
            sim.set_external_load(force)
        input(f"Apply {force:.3f} N, let it settle, then press Enter... ")
        mean, std, n = collect()
        noisy = "  (noisy: is it still swinging?)" if idle_noise and std > 3 * idle_noise else ""
        points.append(LoadPoint(force, mean, std, n, text))
        print(f"  {force:.3f} N: {mean:.0f} counts (std {std:.1f}){noisy}")
    if sim:
        sim.set_external_load(0.0)
    return points


def _fit_load_points(points: list, cfg: Config):
    """Fit and print the result; None if there are not enough points."""
    from vsbc.calibration import fit_load_calibration

    if len(points) < 2:
        print("Need at least one known load.")
        return None
    fit = fit_load_calibration(points, cfg.loadcell.capacity_n)
    print(f"\nScale {fit.scale:.4f} counts/N, zero {fit.offset:.0f} counts, R2 {fit.r2:.6f}")
    pct = f" ({fit.max_residual_pct_fs:.3f} % of {cfg.loadcell.capacity_n:g} N)" if fit.max_residual_pct_fs is not None else ""
    print(f"Largest residual {fit.max_residual_n:.3f} N{pct}")
    return fit


def _calibrate_load_no_mega(cfg: Config, seconds: float = 3.0) -> int:
    """Calibrate with only the Pi and the HX711. The Mega gets the result the next time the app connects to it."""
    from vsbc.calibration import collect_raw_reader, save_pending_load, save_record
    from vsbc.loadcell_pi import LoadCellUnavailable, PiHX711

    lc = cfg.loadcell
    try:
        reader = PiHX711(lc.dout_pin, lc.sck_pin, lc.gain)
    except LoadCellUnavailable as e:
        print(f"Cannot read the load cell on the Pi's GPIO pins: {e}", file=sys.stderr)
        return 1
    try:
        print("Load-cell calibration without the Mega. Apply the known loads in the tension direction "
              "(for example hang weights).")
        input("Remove all load, then press Enter... ")
        points = _collect_load_points(cfg, lambda: collect_raw_reader(reader, seconds))
    finally:
        reader.close()
    fit = _fit_load_points(points, cfg)
    if fit is None:
        return 1
    if not confirm("Keep this calibration? It is stored on the Mega the next time the app connects to it."):
        return 1
    save_pending_load(cfg.pending_load_file, fit)
    path = save_record(cfg.calibration_dir, "load", {
        "points": points, "fit": fit, "capacity_n": lc.capacity_n, "gravity": lc.gravity,
        "source": "pi", "gain": lc.gain, "applied": False, "firmware": None, "port": None,
    })
    print(f"Kept: it will be stored on the Mega at the next connection. Record: {path}")
    print("The overload cutoff (max_load) is set on the Mega: check it in the Calibrate tab after connecting.")
    return 0


def _calibrate_load(m: Machine, cfg: Config) -> int:
    from vsbc.calibration import apply_load_fit, collect_raw, save_record

    if not m.status().loadcell_ok:
        print(f"Load cell not responding: check the HX711 wiring to the {'Pi' if m.loadcell_source == 'pi' else 'Mega'}.")
        return 1
    print("Load-cell calibration. Apply the known loads in the tension direction (for example hang weights).")
    input("Remove all load, then press Enter to tare... ")
    reply = m.tare()
    print(f"  Tare offset {reply.get('offset')} counts, noise {reply.get('noise')} counts")
    points = _collect_load_points(cfg, lambda: collect_raw(m), sim=m.sim)
    fit = _fit_load_points(points, cfg)
    if fit is None:
        return 1
    if not confirm("Save this calibration to the machine?"):
        return 1
    apply_load_fit(m, fit)
    if m.settings.get("max_load", 0.0) <= 0:
        default = 0.9 * cfg.loadcell.capacity_n
        if confirm(f"The overload cutoff (max_load) is off. Set it to {default:g} N?", default=True):
            m.set("max_load", default)
            m.save()
    path = save_record(cfg.calibration_dir, "load", {
        "points": points, "fit": fit, "capacity_n": cfg.loadcell.capacity_n,
        "gravity": cfg.loadcell.gravity, "firmware": asdict(m.info) if m.info else None, "port": m.port,
    })
    print(f"Saved to the machine. Record: {path}")
    return 0


def _calibrate_travel(m: Machine, cfg: Config) -> int:
    from vsbc.calibration import TravelTrial, apply_travel, save_record, travel_correction

    old = m.refresh_settings()["steps_per_mm"]
    print(f"Travel calibration. Current steps/mm: {old:.4f}")
    distance = ask_float("Distance to move (mm)", 20.0)
    rate = ask_float("Rate (mm/s)", 1.0)
    trials = []
    while True:
        input(f"Zero the dial indicator / calipers, then press Enter to move +{distance:g} mm... ")
        start_true = m.sim.true_position_mm() if m.sim else None
        end = m.move_and_wait(distance, rate_mm_s=rate)
        if end.fields.get("reason") != "DONE":
            print(f"The move ended early ({end.fields.get('reason')}).")
            return 1
        commanded = end.number("moved")
        suggested = round(m.sim.true_position_mm() - start_true, 4) if m.sim else None
        measured = ask_float("Measured travel (mm)", suggested)
        trials.append(TravelTrial(commanded, measured))
        if confirm("Move back to the start?", default=True):
            m.move_and_wait(-distance, rate_mm_s=rate)
        if not confirm("Another trial?"):
            break
    result = travel_correction(old, trials)
    print(f"\nMeasured/commanded = {result.ratio:.5f}; steps/mm {old:.4f} -> {result.new_steps_per_mm:.4f}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    if not confirm("Save the new steps/mm to the machine?"):
        return 1
    apply_travel(m, result.new_steps_per_mm)
    path = save_record(cfg.calibration_dir, "travel", {
        "trials": trials, "result": result, "rate_mm_s": rate,
        "firmware": asdict(m.info) if m.info else None, "port": m.port,
    })
    print(f"Saved. Record: {path}\nPositions in mm changed slightly: set zero at the reference position again.")
    return 0


def cmd_calibrate(args, cfg: Config) -> int:
    if args.no_mega:
        if args.what != "load":
            raise ValueError("--no-mega only works with 'calibrate load': the travel calibration moves the motors")
        return _calibrate_load_no_mega(cfg)
    m = _connect(args, cfg)
    try:
        return _calibrate_load(m, cfg) if args.what == "load" else _calibrate_travel(m, cfg)
    finally:
        m.disconnect()


def cmd_analyze(args, cfg: Config) -> int:
    from vsbc.analysis import analyze, load_csv
    from vsbc.plotting import save_test_plot

    path = Path(args.path)
    folder = path if path.is_dir() else path.parent
    csv_path = path / "data.csv" if path.is_dir() else path
    cols = load_csv(csv_path)
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    spec = meta.get("specimen") or {}
    area = args.area or spec.get("area_mm2")
    gauge = args.gauge or spec.get("gauge_length_mm")
    ext, load = cols["extension_mm"], cols["load_N"]
    summary = analyze(cols["time_s"], ext, load, area_mm2=area, gauge_length_mm=gauge,
                      break_drop_pct=args.break_drop, break_min_n=args.break_min,
                      fit_lo_frac=args.fit[0], fit_hi_frac=args.fit[1], stop_reason=meta.get("stop_reason"))
    (folder / "summary.json").write_text(json.dumps(summary.to_dict(), indent=2))
    (folder / "summary.txt").write_text(f"Specimen: {spec.get('specimen_id', folder.name)}\n\n{summary.to_text()}\n")
    stress = load / area if area else None
    strain = ext / gauge if gauge else None
    save_test_plot(folder / "plot.png", ext, load, summary, title=str(spec.get("specimen_id", folder.name)),
                   stress_mpa=stress if area and gauge else None,
                   strain=strain if area and gauge else None)
    print(summary.to_text())
    print(f"\nUpdated {folder / 'summary.json'} and plot.png")
    return 0


def cmd_flash(args, cfg: Config) -> int:
    from vsbc.firmware import compile_firmware, upload_firmware

    if args.compile_only:
        compile_firmware(args.cli)
        return 0
    port = args.port or cfg.serial.port or find_port()
    upload_firmware(port, args.cli)
    time.sleep(1.0)
    m = Machine(cfg)
    info = m.connect(port, stream=False, watchdog=False, loadcell=False)
    print(f"Firmware on {port}: {info.name} {info.version} (protocol {info.protocol})")
    m.disconnect()
    return 0


def cmd_loadcell(args, cfg: Config) -> int:
    """Bring-up check of the HX711 on the Pi's GPIO pins. The Mega is not used."""
    from vsbc.loadcell_pi import LoadCellUnavailable, PiHX711

    lc = cfg.loadcell
    try:
        reader = PiHX711(lc.dout_pin, lc.sck_pin, lc.gain)
    except LoadCellUnavailable as e:
        print(f"Cannot read the load cell on the Pi's GPIO pins: {e}", file=sys.stderr)
        return 1
    print(f"HX711 on GPIO{lc.dout_pin} (DAT) / GPIO{lc.sck_pin} (CLK), gain {lc.gain}. Raw counts for {args.seconds:g} s:")
    good = 0
    start = last_print = time.monotonic()
    try:
        while time.monotonic() - start < args.seconds:
            raw = reader.read()
            if raw is None:
                continue
            good += 1
            now = time.monotonic()
            if now - last_print >= 0.25:
                last_print = now
                print(f"  {raw:9d}", flush=True)
    finally:
        elapsed = time.monotonic() - start
        reader.close()
    if good == 0:
        print("No readings: check the wiring (VDD to 3.3V, VCC to 5V, GND, DAT, CLK) and the pin numbers in config.toml.")
        return 1
    total = good + reader.discarded
    print(f"{good} readings in {elapsed:.1f} s = {good / elapsed:.1f} per second; "
          f"{reader.discarded} discarded ({100 * reader.discarded / total:.1f} %).")
    return 0


# ------------------------------------------------------------------- parser --

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vsbc", description="VSBC tensile tester")
    p.add_argument("--version", action="version", version=f"vsbc {__version__}")
    p.add_argument("--config", help="config TOML file (default: config.toml if present)")
    p.add_argument("--output-dir", help="folder for tests, calibration records and logs (default: Outputs/)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command")

    def hardware(sp):
        sp.add_argument("--port", help="serial port, e.g. /dev/ttyACM0 or COM5 (default: auto-detect)")
        sp.add_argument("--sim", action="store_true", help="use the built-in simulator instead of the Mega")
        sp.add_argument("--loadcell", choices=LOADCELL_SOURCES,
                        help="where the HX711 is wired (default: 'source' in config.toml, normally mega)")

    g = sub.add_parser("gui", help="touchscreen GUI (the default)")
    hardware(g)
    g.add_argument("--fullscreen", action="store_true")

    hardware(sub.add_parser("terminal", help="serial terminal with a session log"))

    t = sub.add_parser("test", help="run a pull-to-failure test without the GUI")
    hardware(t)
    t.add_argument("--specimen", required=True, help="specimen ID (used in the folder name)")
    t.add_argument("--rate", type=float, help="crosshead rate, mm/min")
    t.add_argument("--max-ext", type=float, help="maximum extension, mm")
    t.add_argument("--stop-load", type=float, help="stop at this load, N (0 = off)")
    t.add_argument("--break-drop", type=float, help="break = load this %% below its peak (0 = off)")
    t.add_argument("--break-min", type=float, help="peak load needed before break detection arms, N")
    t.add_argument("--area", type=float, help="cross-section area, mm^2 (for stress)")
    t.add_argument("--gauge", type=float, help="gauge length, mm (for strain)")
    t.add_argument("--no-tare", action="store_true", help="do not tare before the test")
    t.add_argument("--return", dest="return_after", action="store_true", help="return to the start afterwards")
    t.add_argument("--operator", default="")
    t.add_argument("--notes", default="")
    t.add_argument("-y", "--yes", action="store_true", help="start without confirming warnings")

    c = sub.add_parser("calibrate", help="calibrate the load cell or the crosshead travel")
    hardware(c)
    c.add_argument("what", choices=["load", "travel"])
    c.add_argument("--no-mega", action="store_true",
                   help="calibrate the load cell on the Pi alone; the Mega gets the result at the next connection")

    a = sub.add_parser("analyze", help="recompute the summary and plot of a test")
    a.add_argument("path", help="test folder or its data.csv")
    a.add_argument("--break-drop", type=float, default=40.0, help="break threshold, %% below peak")
    a.add_argument("--break-min", type=float, default=0.0, help="minimum peak for a break, N")
    a.add_argument("--fit", nargs=2, type=float, default=(0.10, 0.40), metavar=("LO", "HI"),
                   help="stiffness fit window as fractions of the peak load")
    a.add_argument("--area", type=float, help="override the cross-section area, mm^2")
    a.add_argument("--gauge", type=float, help="override the gauge length, mm")

    f = sub.add_parser("flash", help="compile and upload the firmware with arduino-cli")
    f.add_argument("--port")
    f.add_argument("--cli", help="path to arduino-cli")
    f.add_argument("--compile-only", action="store_true")

    sub.add_parser("ports", help="list serial ports")

    lc = sub.add_parser("loadcell", help="check the HX711 wired to the Pi's GPIO pins (raw counts, sample rate)")
    lc.add_argument("--seconds", type=float, default=5.0, help="how long to read")
    return p


COMMANDS = {
    "gui": cmd_gui, "terminal": cmd_terminal, "test": cmd_test, "calibrate": cmd_calibrate,
    "analyze": cmd_analyze, "flash": cmd_flash, "ports": cmd_ports, "loadcell": cmd_loadcell,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.output_dir:
        cfg.output_dir = Path(args.output_dir).resolve()
    if getattr(args, "loadcell", None):
        cfg.loadcell.source = args.loadcell
    setup_logging(cfg.logs_dir, logging.DEBUG if args.verbose else logging.INFO)
    command = args.command or "gui"
    log.info("vsbc %s: %s", __version__, command)
    try:
        return COMMANDS[command](args, cfg) or 0
    except KeyboardInterrupt:
        return 130
    except (PortNotFound, LinkClosed, FirmwareMismatch, DeviceError, PreflightError,
            FirmwareToolError, TimeoutError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        if isinstance(e, FirmwareMismatch) and e.banner:
            print("The board printed:\n  " + "\n  ".join(e.banner[:8]), file=sys.stderr)
        return 1
