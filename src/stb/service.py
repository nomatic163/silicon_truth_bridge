from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from stb import API_VERSION
from stb.backends.base import Backend
from stb.errors import StbError
from stb.models import LimitsReceipt, MetricsReceipt, Receipt, Response, Status
from stb.response_limits import compact_json_bytes, response_limit_error


class StbService:
    def __init__(
        self,
        backend: Backend,
        context_id: str | None = None,
        normal_response_bytes: int = 4 << 20,
        hard_response_bytes: int = 16 << 20,
        default_timeout_sec: float = 120.0,
    ) -> None:
        self.backend = backend
        self.context_id = context_id
        self.normal_response_bytes = normal_response_bytes
        self.hard_response_bytes = hard_response_bytes
        self.default_timeout_sec = default_timeout_sec
        self.request_id_override: str | None = None

    def _call(
        self,
        operation: str,
        func: Callable[[], Any],
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = self.request_id_override or f"req-{uuid.uuid4().hex[:16]}"
        started = time.perf_counter()
        input_bytes = len(compact_json_bytes(args or {}))
        self.backend.reset_request_metrics()
        try:
            self.backend.set_request_deadline(started + self.default_timeout_sec)
            # Reload operations must remain usable after an external file change.
            if operation != "wave_manage":
                self.backend.check_resources(operation, args)
            data = func()
            status = self._status_from_data(data)
            error = None
        except StbError as exc:
            data = None
            status = Status.FAILED
            error = exc.as_dict()
        finally:
            self.backend.set_request_deadline(None)
        python_done = time.perf_counter()
        duration_ms = (python_done - started) * 1000
        request_metrics = self.backend.request_metrics()
        wave_id = data.get("wave_id") if isinstance(data, dict) else None
        limits = self._limits_from_data(data)
        response = Response(
            status=status,
            data=data,
            error=error,
            receipt=Receipt(
                api_version=API_VERSION,
                request_id=request_id,
                context_id=self.context_id,
                worker_generation=getattr(self.backend, "generation", None),
                backend=self.backend.name,
                design_fingerprint=getattr(
                    self.backend, "design_fingerprint", None
                ),
                wave_id=wave_id,
                wave_generation=self.backend.wave_generation(wave_id),
                verdi_version=getattr(self.backend, "verdi_version", None),
                verdi_compatibility=getattr(
                    self.backend, "verdi_compatibility", None
                ),
                limits=limits,
                metrics=MetricsReceipt(
                    duration_ms=duration_ms,
                    total_ms=duration_ms,
                    python_ms=max(
                        0.0,
                        duration_ms - float(request_metrics.get("npi_ms", 0)),
                    ),
                    input_bytes=input_bytes,
                    **request_metrics,
                ),
            ),
        )
        result = response.model_dump(mode="json", exclude_none=True)
        serialization_started = time.perf_counter()
        metrics = result["receipt"]["metrics"]
        metrics["total_ms"] = (time.perf_counter() - started) * 1000
        metrics["duration_ms"] = metrics["total_ms"]
        size = self._finalize_response_metrics(result, serialization_started)
        if size > self.normal_response_bytes:
            error = response_limit_error(
                size,
                self.normal_response_bytes,
                self.hard_response_bytes,
            )
            failed_result = Response(
                status=Status.FAILED,
                error=error.as_dict(),
                receipt=Receipt(
                    api_version=API_VERSION,
                    request_id=request_id,
                    context_id=self.context_id,
                    worker_generation=getattr(self.backend, "generation", None),
                    backend=self.backend.name,
                    design_fingerprint=getattr(
                        self.backend, "design_fingerprint", None
                    ),
                    wave_id=wave_id,
                    wave_generation=self.backend.wave_generation(wave_id),
                    verdi_version=getattr(self.backend, "verdi_version", None),
                    verdi_compatibility=getattr(
                        self.backend, "verdi_compatibility", None
                    ),
                    limits=LimitsReceipt(
                        truncated=True,
                        termination_reason="response_byte_limit",
                    ),
                    metrics=MetricsReceipt(
                        duration_ms=(time.perf_counter() - started) * 1000,
                        total_ms=(time.perf_counter() - started) * 1000,
                        python_ms=max(
                            0.0,
                            duration_ms - float(request_metrics.get("npi_ms", 0)),
                        ),
                        input_bytes=input_bytes,
                        **request_metrics,
                    ),
                ),
            ).model_dump(mode="json", exclude_none=True)
            failed_metrics = failed_result["receipt"]["metrics"]
            failed_metrics["total_ms"] = (time.perf_counter() - started) * 1000
            failed_metrics["duration_ms"] = failed_metrics["total_ms"]
            self._finalize_response_metrics(failed_result, serialization_started)
            return failed_result
        return result

    def _finalize_response_metrics(
        self,
        result: dict[str, Any],
        serialization_started: float,
    ) -> int:
        metrics = result["receipt"]["metrics"]
        size = 0
        for _ in range(4):
            metrics["serialization_ms"] = (
                time.perf_counter() - serialization_started
            ) * 1000
            encoded = compact_json_bytes(result)
            next_size = len(encoded)
            if metrics.get("response_bytes") == next_size and size == next_size:
                return next_size
            metrics["response_bytes"] = next_size
            size = next_size
        encoded = compact_json_bytes(result)
        size = len(encoded)
        metrics["response_bytes"] = size
        return size

    def _status_from_data(self, data: Any) -> Status:
        if self._contains_truncation(data):
            return Status.PARTIAL
        if self._contains_item_failure(data):
            return Status.PARTIAL
        return Status.COMPLETE

    def _contains_truncation(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        if data.get("truncated") is True or data.get("next_cursor"):
            return True
        limits = data.get("limits")
        if isinstance(limits, dict) and (
            limits.get("truncated") is True or limits.get("next_cursor")
        ):
            return True
        for key in ("items", "values", "signals", "results"):
            values = data.get(key)
            if isinstance(values, list) and any(
                isinstance(item, dict)
                and (item.get("truncated") is True or item.get("next_cursor"))
                for item in values
            ):
                return True
        return False

    def _contains_item_failure(self, data: Any) -> bool:
        if isinstance(data, list):
            return any(
                isinstance(item, dict) and item.get("ok") is False for item in data
            )
        if not isinstance(data, dict):
            return False
        for key in ("items", "values", "signals", "results"):
            values = data.get(key)
            if isinstance(values, list) and any(
                isinstance(item, dict) and item.get("ok") is False for item in values
            ):
                return True
        return False

    def _limits_from_data(self, data: Any) -> LimitsReceipt:
        if not isinstance(data, dict):
            return LimitsReceipt()
        raw = data.get("limits") if isinstance(data.get("limits"), dict) else data
        returned = raw.get("returned")
        if returned is None:
            returned = self._infer_returned(data)
        return LimitsReceipt(
            truncated=bool(raw.get("truncated", False)),
            termination_reason=raw.get("termination_reason"),
            scanned=raw.get("scanned"),
            returned=returned,
            next_cursor=data.get("next_cursor") or raw.get("next_cursor"),
        )

    def _infer_returned(self, data: dict[str, Any]) -> int | None:
        for key in (
            "objects",
            "items",
            "values",
            "signals",
            "rows",
            "events",
            "transactions",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return None

    def catalog(self, kind: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call("catalog", lambda: self.backend.catalog(kind, filters or {}))

    def object_resolve(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._call("object_resolve", lambda: self.backend.object_resolve(args), args)

    def object_get(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._call("object_get", lambda: self.backend.object_get(args), args)

    def object_query(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._call("object_query", lambda: self.backend.object_query(args), args)

    def object_traverse(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._call("object_traverse", lambda: self.backend.object_traverse(args), args)
