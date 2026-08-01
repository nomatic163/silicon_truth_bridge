from __future__ import annotations

import json
from typing import Any

from stb.errors import StbError


def compact_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def response_size(value: Any) -> int:
    return len(compact_json_bytes(value))


def response_limit_error(
    size: int,
    normal_limit: int,
    hard_limit: int,
) -> StbError:
    boundary = "hard" if size > hard_limit else "normal"
    return StbError(
        "limit_exceeded",
        f"response exceeds the {boundary} response byte limit",
        {
            "response_bytes": size,
            "normal_limit_bytes": normal_limit,
            "hard_limit_bytes": hard_limit,
            "remediation": (
                "request a smaller page or use artifact.export for the operation"
            ),
        },
    )


def enforce_response_limit(
    value: dict[str, Any],
    normal_limit: int,
    hard_limit: int,
) -> int:
    size = response_size(value)
    if size > normal_limit:
        raise response_limit_error(size, normal_limit, hard_limit)
    return size
