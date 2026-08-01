from __future__ import annotations

from abc import ABC, abstractmethod
import time
from typing import Any

from stb.errors import StbError


class Backend(ABC):
    name = "base"

    @abstractmethod
    def catalog(self, kind: str, filters: dict[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def object_resolve(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def object_get(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def object_query(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def object_traverse(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def set_request_deadline(self, deadline: float | None) -> None:
        self._request_deadline = deadline

    def soft_timed_out(self) -> bool:
        deadline = getattr(self, "_request_deadline", None)
        return deadline is not None and time.perf_counter() >= deadline

    def check_resources(
        self, operation: str | None = None, args: dict[str, Any] | None = None
    ) -> None:
        return None

    def wave_generation(self, wave_id: str | None) -> int | None:
        return None

    def reset_request_metrics(self) -> None:
        return None

    def request_metrics(self) -> dict[str, int | float]:
        return {
            "npi_calls": 0,
            "npi_ms": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

    def validate_ref(self, ref: dict[str, Any]) -> None:
        context_id = getattr(self, "context_id", None)
        generation = getattr(self, "generation", None)
        if ref.get("context_id") != context_id or ref.get("worker_generation") != generation:
            raise StbError(
                "stale_object_id",
                "object reference does not belong to the current context generation",
                {
                    "reference_context_id": ref.get("context_id"),
                    "current_context_id": context_id,
                    "reference_worker_generation": ref.get("worker_generation"),
                    "current_worker_generation": generation,
                },
            )

    def release_objects(self, object_ids: list[str]) -> Any:
        raise NotImplementedError

    def wave_manage(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def wave_value(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def wave_changes(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def connectivity_direct(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def trace(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def trace_active_driver(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def trace_value_origin(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def wave_compute(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def source_context(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def assertion_structure(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError

    def mapping(self, args: dict[str, Any]) -> Any:
        raise NotImplementedError
