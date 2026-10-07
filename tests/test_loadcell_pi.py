"""Load cell read by the host: the Pi HX711 reader (against a fake lgpio), the
feeder thread, and the Mega side of it (`LC <raw>`, `lc_host`) in the simulator."""

import time

import pytest

from vsbc import loadcell_pi
from vsbc.config import load_config
from vsbc.loadcell_pi import LoadCellFeeder, LoadCellUnavailable, PiHX711
from vsbc.machine import Machine
from vsbc.sim import Specimen
from tests.conftest import FAST_SPECIMEN

DOUT, SCK = 5, 6


class FakeLgpio:
    """An HX711 behind the lgpio calls PiHX711 uses."""

    SET_PULL_UP = 32
    error = RuntimeError

    def __init__(self, counts=0, connected=True):
        self.counts = counts
        self.connected = connected
        self.closed = False
        self.freed = []
        self.pulses = 0          # clock pulses of the reading in progress
        self.sck = 0
        self.ready = True

    def gpiochip_open(self, number):
        if number != 0:
            raise self.error("no such chip")
        return 7

    def gpio_get_chip_info(self, handle):
        return (0, 54, "gpiochip0", "pinctrl-rp1")

    def gpio_claim_output(self, handle, pin, level=0):
        return 0

    def gpio_claim_input(self, handle, pin, flags=0):
        return 0

    def gpio_free(self, handle, pin):
        self.freed.append(pin)

    def gpiochip_close(self, handle):
        self.closed = True

    def gpio_write(self, handle, pin, level):
        if level and not self.sck:
            self.pulses += 1
            if self.pulses == 25:
                self.ready = False      # conversion read: DOUT goes high
        self.sck = level

    def gpio_read(self, handle, pin):
        if not self.connected:
            return 1                    # pull-up: never ready
        if not self.ready:
            return 1
        if 1 <= self.pulses <= 24 and self.sck:
            return (self.counts & 0xFFFFFF) >> (24 - self.pulses) & 1
        return 0                        # ready, waiting for the first pulse

    def next_conversion(self, counts):
        self.counts = counts
        self.pulses = 0
        self.ready = True


@pytest.mark.parametrize("counts", [0, 1, 84213, 8388607, -1, -84213, -8388608])
def test_reads_signed_24_bit_counts(counts):
    gpio = FakeLgpio(counts)
    hx = PiHX711(DOUT, SCK, lgpio=gpio)
    assert hx.read() == counts
    assert gpio.pulses == 25            # 24 data bits + 1 pulse for gain 128
    hx.close()
    assert gpio.closed and set(gpio.freed) == {DOUT, SCK}


def test_gain_sets_the_number_of_extra_pulses():
    gpio = FakeLgpio(1234)
    assert PiHX711(DOUT, SCK, gain=64, lgpio=gpio).read() == 1234
    assert gpio.pulses == 27
    with pytest.raises(ValueError):
        PiHX711(DOUT, SCK, gain=100, lgpio=FakeLgpio())


def test_no_amplifier_gives_no_reading():
    hx = PiHX711(DOUT, SCK, lgpio=FakeLgpio(connected=False))
    assert hx.read(timeout=0.05) is None
    assert hx.discarded == 0


def test_a_slow_clock_pulse_discards_the_reading_and_the_next(monkeypatch):
    gpio = FakeLgpio(5000)
    hx = PiHX711(DOUT, SCK, lgpio=gpio)
    monkeypatch.setattr(loadcell_pi, "MAX_PULSE_NS", -1)     # every pulse counts as too slow
    assert hx.read() is None
    monkeypatch.undo()
    gpio.next_conversion(5001)
    assert hx.read() is None            # first reading after a possible power-down
    gpio.next_conversion(5002)
    assert hx.read() == 5002
    assert hx.discarded == 2


def test_unavailable_without_a_gpio_chip():
    class NoChips(FakeLgpio):
        def gpiochip_open(self, number):
            raise self.error("no such chip")

    with pytest.raises(LoadCellUnavailable):
        PiHX711(DOUT, SCK, lgpio=NoChips())


class SimReader:
    """Plays the Pi's HX711: reads the simulator's load cell at 80 samples/s."""

    def __init__(self, sim):
        self.sim = sim
        self.connected = True
        self.closed = False
        self.discarded = 0

    def read(self):
        time.sleep(1 / 80)
        return self.sim.adc_counts() if self.connected else None

    def close(self):
        self.closed = True


def test_feeder_passes_readings_on_and_closes_the_reader(sim):
    reader = SimReader(sim)
    got = []
    feeder = LoadCellFeeder(reader, got.append)
    feeder.start()
    time.sleep(0.2)
    feeder.stop()
    assert len(got) >= 5 and feeder.samples == len(got)
    assert reader.closed


@pytest.fixture
def fed(config, sim):
    """A Machine whose load cell is read by the host and sent to the (simulated) Mega."""
    reader = SimReader(sim)
    m = Machine(config, loadcell_reader=reader)
    m.connect(sim=sim)
    yield m, reader
    m.disconnect()


def test_host_fed_load_cell_reaches_the_mega(fed, sim):
    m, reader = fed
    assert m.loadcell_source == "pi"
    assert m.settings["lc_host"] == 1
    assert m.command("ID").get("loadcell") == "HOST"
    time.sleep(0.4)
    st = m.status()
    assert st.loadcell_ok and st.calibrated
    assert 50 < st.sample_rate_hz < 110
    sim.set_external_load(100.0)
    time.sleep(0.2)
    assert m.latest.load_n == pytest.approx(100.0, abs=1.0)
    assert not any("not responding" in w for w in m.warnings)
    m.disconnect()
    assert reader.closed


def test_lc_lines_stay_out_of_the_console_and_the_log(config, sim):
    m = Machine(config, loadcell_reader=SimReader(sim))
    sent = []
    m.add_raw_listener(lambda text, was_sent: sent.append(text) if was_sent else None)
    m.connect(sim=sim)
    time.sleep(0.3)
    m.disconnect()
    assert sent and not any(line.startswith("LC ") for line in sent)
    log_text = next(config.logs_dir.glob("*_serial.log")).read_text()
    assert "SET lc_host 1" in log_text and "LC " not in log_text.replace("LOADCELL", "")


def test_host_fed_break_detection(fed):
    m, _ = fed
    m.zero()
    m.set("break_drop", 40)
    m.set("break_min", 5)
    m.set_rate(5.0)
    m.run(5.0)
    end = m.wait_move_end(timeout=5)
    assert end.fields["reason"] == "BREAK"
    assert end.number("peak") == pytest.approx(400, rel=0.08)


def test_host_fed_overload_cutoff(fed):
    m, _ = fed
    m.zero()
    m.set("max_load", 200)
    m.set_rate(5.0)
    m.run(5.0)
    assert m.wait_move_end(timeout=5).fields["reason"] == "OVERLOAD"


def test_mega_stops_the_test_when_the_host_stops_sending(fed, sim):
    m, reader = fed
    m.zero()
    sim.remount_specimen(Specimen(**{**FAST_SPECIMEN, "break_extension_mm": 50.0, "extension_at_peak_mm": 40.0}))
    m.set_rate(1.0)
    m.run(5.0)
    time.sleep(0.2)
    reader.connected = False            # the Pi's reader hangs; pings keep going
    end = m.wait_move_end(timeout=3)
    assert end.fields["reason"] == "LOADCELL"
    assert not m.status().loadcell_ok


def test_lc_is_ignored_unless_host_fed(machine, sim):
    assert machine.settings["lc_host"] == 0
    machine.send_raw("LC 8000000")
    time.sleep(0.2)
    assert abs(machine.latest.load_n) < 2.0          # still the Mega's own HX711
    assert machine.command("ID").get("loadcell") == "HX711"


def test_tare_with_a_host_fed_load_cell(fed, sim):
    m, _ = fed
    time.sleep(0.2)
    reply = m.tare()
    assert reply.number("offset") == pytest.approx(sim.true_offset, abs=60)


def test_falls_back_to_the_mega_when_gpio_is_unavailable(config, monkeypatch):
    def unavailable(*args, **kwargs):
        raise LoadCellUnavailable("the lgpio module is not installed")

    monkeypatch.setattr(loadcell_pi, "PiHX711", unavailable)
    m = Machine(config)
    assert config.loadcell.source == "pi"            # the default
    # A real port is needed for this path, so drive the private step directly on a simulated link.
    m.connect(sim=True)
    try:
        m.sim_backup, m.sim = m.sim, None            # pretend it is a real Mega
        m.warnings = []
        m._start_host_load_cell()
        assert m.loadcell_source == "mega"
        assert any("Using the HX711 wired to the Mega" in w for w in m.warnings)
        assert m.refresh_settings()["lc_host"] == 0
    finally:
        m.sim = m.sim_backup
        m.disconnect()


def test_source_must_be_pi_or_mega(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[loadcell]\nsource = "mega"\ndout_pin = 17\n')
    cfg = load_config(path)
    assert cfg.loadcell.source == "mega" and cfg.loadcell.dout_pin == 17
    path.write_text('[loadcell]\nsource = "arduino"\n')
    with pytest.raises(ValueError, match="loadcell.source"):
        load_config(path)


# ------------------------------------------- calibration without the Mega --

class WeightReader:
    """A load cell on the Pi: `counts` is whatever is hanging on it right now."""

    def __init__(self, counts=None):
        self.counts = counts
        self.closed = False

    def read(self):
        time.sleep(0.002)
        return self.counts

    def close(self):
        self.closed = True


def test_collect_raw_reader_averages_the_readings():
    from vsbc.calibration import collect_raw_reader

    mean, std, n = collect_raw_reader(WeightReader(84213), seconds=0.1)
    assert mean == 84213 and std == 0 and n >= 5


def test_collect_raw_reader_times_out_without_readings(monkeypatch):
    from vsbc import calibration

    clock = iter(x * 2.0 for x in range(100))
    monkeypatch.setattr(calibration.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        calibration.collect_raw_reader(WeightReader(None), seconds=0.1)


SCALE, OFFSET = 4194.304, 84213      # counts per newton, counts at zero load


def _calibrate_without_mega(config, monkeypatch, answers):
    from vsbc import cli

    reader = WeightReader(OFFSET)
    monkeypatch.setattr(loadcell_pi, "PiHX711", lambda *args, **kwargs: reader)
    answers = iter(answers)

    def fake_input(prompt=""):
        answer = next(answers)
        if prompt.startswith("Apply "):                 # "Apply 98.066 N, let it settle..."
            reader.counts = round(OFFSET + SCALE * float(prompt.split()[1]))
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return cli._calibrate_load_no_mega(config, seconds=0.05), reader


def test_calibration_without_the_mega_is_kept_for_later(config, monkeypatch):
    from vsbc.calibration import latest_record, load_pending_load

    # Enter (no load), two known loads with Enter after applying each, Enter to finish, "y" to keep.
    code, reader = _calibrate_without_mega(config, monkeypatch, ["", "10 N", "", "5 kg", "", "", "y"])
    assert code == 0 and reader.closed
    pending = load_pending_load(config.pending_load_file)
    assert pending["scale"] == pytest.approx(SCALE, rel=1e-4)
    assert pending["offset"] == pytest.approx(OFFSET, abs=2)
    _, record = latest_record(config.calibration_dir, "load")
    assert record["applied"] is False and record["source"] == "pi" and len(record["points"]) == 3


def test_calibration_without_the_mega_can_be_declined(config, monkeypatch):
    code, _ = _calibrate_without_mega(config, monkeypatch, ["", "10 N", "", "", "n"])
    assert code == 1 and not config.pending_load_file.exists()


def test_no_mega_is_only_for_the_load_cell(config):
    from vsbc import cli

    args = cli.build_parser().parse_args(["calibrate", "travel", "--no-mega"])
    with pytest.raises(ValueError, match="no-mega"):
        cli.cmd_calibrate(args, config)


def _write_pending(config, scale=1234.5, offset=4321):
    from vsbc.calibration import LoadFit, save_pending_load

    save_pending_load(config.pending_load_file, LoadFit(scale, offset, 1.0, [], 0.0, None))


def test_pending_calibration_is_stored_on_the_mega_once(config, tmp_path):
    from vsbc.sim import SimulatedMega

    _write_pending(config)
    eeprom = tmp_path / "eeprom.json"
    sim = SimulatedMega(eeprom_path=eeprom)
    m = Machine(config, loadcell_reader=SimReader(sim))
    m.connect(sim=sim)
    try:
        assert m.settings["load_scale"] == pytest.approx(1234.5)
        assert m.settings["load_offset"] == 4321
        assert any("made without the Mega" in w and "max_load" in w for w in m.warnings)
        assert not config.pending_load_file.exists()
    finally:
        m.disconnect()

    # Saved in the EEPROM, and nothing is applied a second time.
    sim = SimulatedMega(eeprom_path=eeprom)
    m = Machine(config, loadcell_reader=SimReader(sim))
    m.connect(sim=sim)
    try:
        assert m.settings["load_scale"] == pytest.approx(1234.5)
        assert not any("made without the Mega" in w for w in m.warnings)
    finally:
        m.disconnect()


def test_pending_calibration_waits_while_the_mega_reads_the_load_cell(config, machine):
    _write_pending(config)
    machine.disconnect()
    machine.connect(sim=True)                       # load cell read by the (simulated) Mega
    assert machine.loadcell_source == "mega"
    assert machine.settings["load_scale"] != pytest.approx(1234.5)
    assert config.pending_load_file.exists()


def test_unreadable_pending_calibration_is_reported_not_applied(config, sim):
    config.pending_load_file.parent.mkdir(parents=True, exist_ok=True)
    config.pending_load_file.write_text("not json")
    m = Machine(config, loadcell_reader=SimReader(sim))
    m.connect(sim=sim)
    try:
        assert any("Could not read the waiting" in w for w in m.warnings)
        assert m.settings["load_scale"] == pytest.approx(sim.true_scale)
    finally:
        m.disconnect()
