"""End-to-end tests of Machine + SerialLink against the simulator (protocol v1)."""

import threading
import time

import pytest

from vsbc import PROTOCOL_VERSION
from vsbc.link import DeviceError
from vsbc.machine import FirmwareMismatch, Machine
from vsbc.protocol import Reply, Sample
from vsbc.sim import SimulatedMega, Specimen
from tests.conftest import FAST_SPECIMEN


def test_handshake_and_status(machine):
    assert machine.info.protocol == PROTOCOL_VERSION
    assert machine.info.simulated
    st = machine.status()
    assert st.state == "IDLE" and st.loadcell_ok and st.calibrated and st.streaming
    assert not st.referenced
    assert any("not referenced" in w for w in machine.warnings)
    assert machine.settings["steps_per_mm"] == pytest.approx(80.0)


def test_samples_stream(machine):
    time.sleep(0.3)
    s = machine.latest
    assert isinstance(s, Sample) and s.state == "I"
    assert abs(s.load_n) < 2.0


def test_rate_limits(machine):
    r = machine.set_rate(0.5)
    assert r.number("actual") == pytest.approx(0.5, rel=1e-3)
    with pytest.raises(DeviceError) as e:
        machine.set_rate(50.0)
    assert e.value.code == "RANGE"


def test_move_and_position(machine):
    end = machine.move_and_wait(1.0, rate_mm_s=5.0)
    assert end.fields["reason"] == "DONE"
    assert end.number("moved") == pytest.approx(1.0)
    assert machine.status().pos_mm == pytest.approx(1.0)
    end = machine.move_and_wait(0.0, rate_mm_s=5.0, absolute=True)
    assert end.fields["reason"] == "DONE" and end.number("pos") == pytest.approx(0.0)


def test_zero_length_move_still_ends(machine):
    end = machine.move_and_wait(0.0, rate_mm_s=1.0)
    assert end.fields["reason"] == "DONE" and end.number("moved") == 0


def test_soft_limits(machine):
    machine.set("max_pos", 5.0)
    with pytest.raises(DeviceError) as e:
        machine.move_to(10.0, rate_mm_s=5.0)
    assert e.value.code == "LIMIT"
    with pytest.raises(DeviceError):
        machine.set("min_pos", 6.0)   # would make the window empty


def test_stop_mid_move(machine):
    machine.move_by(10.0, rate_mm_s=2.0)
    time.sleep(0.3)
    machine.stop()
    end = machine.wait_move_end(timeout=2)
    assert end.fields["reason"] == "STOP"
    assert 0.2 < end.number("moved") < 2.0


def test_any_command_stops_a_move(machine):
    machine.move_by(10.0, rate_mm_s=2.0)
    time.sleep(0.2)
    with pytest.raises(DeviceError) as e:
        machine.set_rate(1.0)
    assert e.value.code == "BUSY"
    assert machine.wait_move_end(timeout=2).fields["reason"] == "STOP"


def test_queries_do_not_stop_a_move(machine):
    machine.move_by(1.0, rate_mm_s=5.0)
    assert machine.status().state == "MOVE"
    machine.refresh_settings()
    assert machine.wait_move_end(timeout=2).fields["reason"] == "DONE"


def test_run_needs_reference(machine):
    with pytest.raises(DeviceError) as e:
        machine.run(5.0)
    assert e.value.code == "NOREF"


def test_run_needs_calibration(config):
    sim = SimulatedMega(calibrated=False)
    m = Machine(config)
    m.connect(sim=sim)
    try:
        m.zero()
        with pytest.raises(DeviceError) as e:
            m.run(5.0)
        assert e.value.code == "UNCAL"
    finally:
        m.disconnect()


def test_run_needs_load_cell(config):
    sim = SimulatedMega(loadcell=False)
    m = Machine(config)
    m.connect(sim=sim)
    try:
        assert not m.status().loadcell_ok
        m.zero()
        with pytest.raises(DeviceError) as e:
            m.run(5.0)
        assert e.value.code == "NOLOAD"
        # Manual moves still work without a load cell (motion bring-up).
        assert m.move_and_wait(0.5, rate_mm_s=5.0).fields["reason"] == "DONE"
    finally:
        m.disconnect()


def _run_test_move(machine, **params):
    machine.zero()
    for key, value in params.items():
        machine.set(key, value)
    machine.set_rate(5.0)
    machine.run(5.0)
    return machine.wait_move_end(timeout=5)


def test_break_detection(machine):
    end = _run_test_move(machine, break_drop=40, break_min=5)
    assert end.fields["reason"] == "BREAK"
    assert end.fields["mode"] == "RUN"
    assert end.number("peak") == pytest.approx(400, rel=0.05)
    assert 1.5 < end.number("moved") < 1.9


def test_stop_load(machine):
    end = _run_test_move(machine, stop_load=250)
    assert end.fields["reason"] == "LOAD"
    # The peak includes the sample that crossed the limit (at most one sample, 25 N, beyond it here).
    assert 250 <= end.number("peak") <= 290


def test_overload_cutoff(machine):
    end = _run_test_move(machine, max_load=200)
    assert end.fields["reason"] == "OVERLOAD"


def test_overload_allows_backing_out(machine):
    _run_test_move(machine, max_load=200)          # stops at about 200 N
    end = machine.move_and_wait(-0.3, rate_mm_s=1.0)
    assert end.fields["reason"] == "DONE"          # unloading is not blocked


def test_load_cell_lost_during_run(machine, sim):
    machine.zero()
    sim.remount_specimen(Specimen(**{**FAST_SPECIMEN, "break_extension_mm": 50.0,
                                     "extension_at_peak_mm": 40.0}))
    machine.set_rate(1.0)
    machine.run(5.0)
    time.sleep(0.2)
    sim.disconnect_loadcell()
    end = machine.wait_move_end(timeout=3)
    assert end.fields["reason"] == "LOADCELL"


def test_watchdog_stops_a_silent_host(machine):
    machine.set("watchdog_ms", 300)
    machine._stop_ping.set()                        # the host "hangs"
    time.sleep(0.3)
    machine.move_by(10.0, rate_mm_s=1.0)
    end = machine.wait_move_end(timeout=3)
    assert end.fields["reason"] == "WATCHDOG"


def test_tare_and_span_calibration(machine, sim):
    sim.set_external_load(0.0)
    machine.tare()
    known = 49.033
    sim.set_external_load(known)
    reply = machine.calibrate_span(known)
    assert reply.number("scale") == pytest.approx(sim.true_scale, rel=0.01)


def test_alias_commands_from_a_terminal(machine):
    seen = []
    done = threading.Event()

    def listener(msg):
        if isinstance(msg, Reply) and msg.command == "RATE":
            seen.append(msg)
            done.set()

    machine.add_listener(listener)
    machine.send_raw("v0.25")
    assert done.wait(2)
    assert seen[0].ok and seen[0].number("rate") == pytest.approx(0.25)


def test_single_motor_mode_clears_reference(machine):
    machine.zero()
    machine.select_motors("A")
    machine.move_and_wait(0.5, rate_mm_s=5.0)
    assert not machine.status().referenced
    machine.select_motors("AB")
    with pytest.raises(DeviceError) as e:
        machine.run(1.0)
    assert e.value.code == "NOREF"


class _LegacySketch:
    """Serial port that behaves like firmware/legacy/dual_stepper_mega: it prints its
    menu after the reset and never answers protocol commands."""

    timeout = 0.05

    def __init__(self):
        self._lines = [b"", b"=== Dual-driver displacement trials (Mega 2560) ===",
                       b"Sketch: dual_stepper_mega", b"  v<mm/s>  set displacement rate (e.g. v0.5, max 10.0)"]

    def write(self, data):
        return len(data)

    def readline(self):
        if self._lines:
            return self._lines.pop(0) + b"\r\n"
        time.sleep(self.timeout)
        return b""

    def close(self):
        pass


def test_legacy_sketch_is_refused_with_its_banner(config, monkeypatch):
    config.serial.ready_timeout_s = 0.3
    monkeypatch.setattr("vsbc.machine.open_serial", lambda port, baud: _LegacySketch())
    m = Machine(config)
    with pytest.raises(FirmwareMismatch) as e:
        m.connect("COM99")
    assert "No answer from vsbc_firmware" in str(e.value)
    assert "Sketch: dual_stepper_mega" in e.value.banner
    assert not m.connected


def test_settings_survive_save_in_eeprom_file(config, tmp_path):
    path = tmp_path / "eeprom.json"
    m = Machine(config)
    m.connect(sim=SimulatedMega(eeprom_path=path))
    m.set("max_load", 123.0)
    m.save()
    m.disconnect()
    m.connect(sim=SimulatedMega(eeprom_path=path))
    try:
        assert m.settings["max_load"] == pytest.approx(123.0)
    finally:
        m.disconnect()
