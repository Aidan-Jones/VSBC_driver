"""Parsing and formatting for serial protocol v1 (docs/protocol.md).

Pure functions and small dataclasses only, so the same code serves the real
link, the simulator and the tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

STATE_NAMES = {"I": "IDLE", "M": "MOVE", "R": "RUN"}


class ProtocolError(ValueError):
    """A line that looks like protocol but cannot be parsed."""


@dataclass(frozen=True)
class Sample:
    """One `D` line."""

    t_ms: int
    pos_mm: float
    load_n: float      # nan when the load cell is missing or not calibrated
    raw: int
    state: str         # 'I', 'M' or 'R'

    @property
    def moving(self) -> bool:
        return self.state in ("M", "R")


@dataclass(frozen=True)
class Reply:
    """An `OK` or `ERR` line."""

    ok: bool
    command: str
    fields: dict = field(default_factory=dict)
    code: str = ""
    message: str = ""

    def get(self, key: str, default=None):
        return self.fields.get(key, default)

    def number(self, key: str, default: float = math.nan) -> float:
        return to_float(self.fields.get(key), default)


@dataclass(frozen=True)
class Event:
    """An `EVT` line."""

    name: str
    fields: dict = field(default_factory=dict)

    def number(self, key: str, default: float = math.nan) -> float:
        return to_float(self.fields.get(key), default)


@dataclass(frozen=True)
class Info:
    """A `#` comment, or any line that is not part of the protocol."""

    text: str


Message = Sample | Reply | Event | Info


def to_float(value, default: float = math.nan) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_fields(tokens: list[str]) -> dict[str, str]:
    """`key=value` tokens to a dict. Values stay strings."""
    fields = {}
    for tok in tokens:
        key, sep, value = tok.partition("=")
        if sep:
            fields[key] = value
    return fields


def parse_line(line: str) -> Message | None:
    """Parse one received line. Returns None for a blank line."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("#"):
        return Info(line[1:].strip())

    parts = line.split()
    head = parts[0]
    if head == "D":
        if len(parts) != 6:
            raise ProtocolError(f"data line needs 5 fields: {line!r}")
        try:
            return Sample(int(parts[1]), float(parts[2]), float(parts[3]), int(parts[4]), parts[5])
        except ValueError as e:
            raise ProtocolError(f"bad data line {line!r}: {e}") from None
    if head in ("OK", "ERR"):
        if len(parts) < 2:
            raise ProtocolError(f"reply without a command: {line!r}")
        command = parts[1].upper()
        if head == "OK":
            return Reply(True, command, parse_fields(parts[2:]))
        code = parts[2] if len(parts) > 2 else "UNKNOWN"
        return Reply(False, command, {}, code, " ".join(parts[3:]))
    if head == "EVT":
        if len(parts) < 2:
            raise ProtocolError(f"event without a name: {line!r}")
        return Event(parts[1].upper(), parse_fields(parts[2:]))
    # Old sketches, bootloader noise, etc.
    return Info(line)


def format_arg(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot send {value!r}")
        return format(value, ".7g")
    return str(value)


def format_command(name: str, *args) -> str:
    """Command line without the trailing newline."""
    return " ".join([name.upper(), *(format_arg(a) for a in args)])


def settings_from_reply(reply: Reply) -> dict[str, float]:
    """`OK GET` fields to numbers."""
    return {k: to_float(v) for k, v in reply.fields.items()}
