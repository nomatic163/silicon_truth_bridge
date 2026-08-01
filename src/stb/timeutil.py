from __future__ import annotations

import re
from decimal import Decimal
from fractions import Fraction
from typing import Any

from stb.errors import StbError

_UNIT_FS = {
    "s": 10**15,
    "ms": 10**12,
    "us": 10**9,
    "ns": 10**6,
    "ps": 10**3,
    "fs": 1,
}
_TIME_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(s|ms|us|ns|ps|fs)\s*$")
_SCALE_RE = re.compile(r"^\s*(\d+)\s*(s|ms|us|ns|ps|fs)\s*$")


def parse_time(value: str | dict[str, Any]) -> Fraction:
    if isinstance(value, dict):
        text = f"{value.get('value', '')}{value.get('unit', '')}"
    elif isinstance(value, str):
        text = value
    else:
        raise StbError("invalid_request", "time must include an explicit unit")
    match = _TIME_RE.match(text)
    if not match:
        raise StbError("invalid_request", f"invalid TimeSpec: {text}")
    number, unit = match.groups()
    return Fraction(Decimal(number)) * _UNIT_FS[unit]


def parse_scale_unit(scale: str) -> tuple[int, str, int]:
    match = _SCALE_RE.match(scale)
    if not match:
        raise StbError("unsupported_capability", f"unsupported FSDB scale unit: {scale}")
    multiplier = int(match.group(1))
    unit = match.group(2)
    return multiplier, unit, multiplier * _UNIT_FS[unit]


def to_raw_tick(value: str | dict[str, Any], scale: str) -> int:
    time_fs = parse_time(value)
    _, _, scale_fs = parse_scale_unit(scale)
    # Value-at-time semantics are value_at_or_before for sub-tick requests.
    return time_fs.numerator // (time_fs.denominator * scale_fs)


def raw_time_point(raw_tick: int, scale: str) -> dict[str, str]:
    multiplier, unit, _ = parse_scale_unit(scale)
    return {
        "ticks": str(raw_tick * multiplier),
        "unit": unit,
        "raw_ticks": str(raw_tick),
        "raw_scale_unit": scale,
    }
