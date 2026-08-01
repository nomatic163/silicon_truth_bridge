from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class StbError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None
    recoverable: bool = True

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }
        if self.details:
            result["details"] = self.details
        return result


def invalid_request(message: str, **details: Any) -> StbError:
    return StbError("invalid_request", message, details or None)


def not_found(kind: str, value: str) -> StbError:
    return StbError(f"{kind}_not_found", f"{kind} not found: {value}")
