from __future__ import annotations

import json
from typing import Any, TextIO


def write_message(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def read_message(stream: TextIO) -> dict[str, Any] | None:
    line = stream.readline()
    if not line:
        return None
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("worker protocol message must be an object")
    return value
