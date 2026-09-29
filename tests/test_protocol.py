import math

import pytest

from vsbc.protocol import Event, Info, ProtocolError, Reply, Sample, format_command, parse_line


def test_data_line():
    s = parse_line("D 123456 12.3450 87.512 1419842 R")
    assert s == Sample(123456, 12.345, 87.512, 1419842, "R")
    assert s.moving


def test_data_line_without_load():
    s = parse_line("D 5 0.0000 nan 0 I")
    assert math.isnan(s.load_n) and s.raw == 0 and not s.moving


def test_malformed_data_line():
    with pytest.raises(ProtocolError):
        parse_line("D 1 2 3")
    with pytest.raises(ProtocolError):
        parse_line("D x 0 0 0 I")


def test_ok_reply_fields():
    r = parse_line("OK RATE rate=0.5000 actual=0.5000 max=10.0000")
    assert isinstance(r, Reply) and r.ok and r.command == "RATE"
    assert r.number("actual") == 0.5 and r.get("max") == "10.0000"


def test_err_reply():
    r = parse_line("ERR GOTO LIMIT target outside the soft limits -100.000 to 100.000 mm")
    assert not r.ok and r.command == "GOTO" and r.code == "LIMIT"
    assert r.message.startswith("target outside")


def test_unknown_command_reply_is_uppercased():
    r = parse_line("ERR foo UNKNOWN unknown command (? for help)")
    assert r.command == "FOO" and r.code == "UNKNOWN"


def test_event():
    e = parse_line("EVT MOVE_END reason=BREAK mode=RUN pos=3.9875 moved=3.9875 time=47.86 peak=401.220")
    assert isinstance(e, Event) and e.name == "MOVE_END"
    assert e.fields["reason"] == "BREAK" and e.number("peak") == pytest.approx(401.22)


def test_info_and_foreign_lines():
    assert parse_line("# hello") == Info("hello")
    assert parse_line("Sketch: dual_stepper_mega") == Info("Sketch: dual_stepper_mega")
    assert parse_line("   ") is None


def test_format_command():
    assert format_command("rate", 0.0833333333) == "RATE 0.08333333"
    assert format_command("SET", "max_load", 450.0) == "SET max_load 450"
    assert format_command("STREAM", "ON") == "STREAM ON"
    assert format_command("SET", "dir_invert", True) == "SET dir_invert 1"
    with pytest.raises(ValueError):
        format_command("MOVE", float("nan"))
