from __future__ import annotations

import subprocess
import threading
import time
import uuid
import select
from collections import defaultdict, deque
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from stb.config import Settings
from stb.cursors import CursorRegistry
from stb.errors import StbError
from stb.launchers import WorkerLauncher, build_launcher
from stb.protocol import read_message, write_message
from stb.timeutil import parse_scale_unit, parse_time


class ContextState(str, Enum):
    OPENING = "opening"
    ACTIVE = "active"
    CRASHED = "crashed"
    CLOSED = "closed"


@dataclass
class WorkerChannel:
    context_id: str
    process: subprocess.Popen[str]
    lock: threading.Lock
    generation: int = 1
    state: ContextState = ContextState.OPENING
    log_path: Path | None = None
    backend_name: str = "fake"
    design_spec: dict[str, Any] | None = None
    wave_specs: list[dict[str, Any]] | None = None
    hard_timeout_sec: float = 300.0
    max_queue: int = 32
    pending_count: int = 0
    pending_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    wave_generations: dict[str, int] = field(default_factory=dict)

    def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        queued_at = time.perf_counter()
        with self.pending_lock:
            if self.pending_count >= self.max_queue + 1:
                raise StbError(
                    "worker_busy",
                    f"worker queue is full for context {self.context_id}",
                    {"queued_limit": self.max_queue},
                )
            self.pending_count += 1
        try:
            return self._call_serialized(method, args, queued_at)
        finally:
            with self.pending_lock:
                self.pending_count -= 1

    def _call_serialized(
        self,
        method: str,
        args: dict[str, Any],
        queued_at: float,
    ) -> dict[str, Any]:
        with self.lock:
            queue_ms = (time.perf_counter() - queued_at) * 1000
            transport_started = time.perf_counter()
            if self.process.poll() is not None:
                self.state = ContextState.CRASHED
                raise StbError("worker_lost", f"worker exited for context {self.context_id}")
            request_id = uuid.uuid4().hex
            assert self.process.stdin is not None
            assert self.process.stdout is not None
            write_message(
                self.process.stdin,
                {"id": request_id, "method": method, "args": args},
            )
            readable, _, _ = select.select(
                [self.process.stdout], [], [], self.hard_timeout_sec
            )
            if not readable:
                self.state = ContextState.CRASHED
                self.process.kill()
                self.process.wait(timeout=2)
                raise StbError(
                    "hard_timeout",
                    f"worker exceeded hard timeout for {method}",
                    {
                        "context_id": self.context_id,
                        "timeout_sec": self.hard_timeout_sec,
                    },
                    recoverable=False,
                )
            response = read_message(self.process.stdout)
            if response is None:
                self.state = ContextState.CRASHED
                raise StbError("worker_lost", f"worker closed pipe for {self.context_id}")
            if response.get("id") != request_id:
                self.state = ContextState.CRASHED
                raise StbError("worker_protocol_error", "worker response ID mismatch")
            if not response.get("ok"):
                error = response.get("error") or {}
                raise StbError(
                    error.get("code", "worker_internal_error"),
                    error.get("message", "worker call failed"),
                    error.get("details"),
                    error.get("recoverable", False),
                )
            result = response.get("result") or {}
            transport_ms = (time.perf_counter() - transport_started) * 1000
            receipt = result.get("receipt") if isinstance(result, dict) else None
            metrics = receipt.get("metrics") if isinstance(receipt, dict) else None
            if isinstance(metrics, dict):
                metrics["queue_ms"] = queue_ms
                metrics["transport_ms"] = transport_ms
                metrics["total_ms"] = float(metrics.get("total_ms", 0)) + queue_ms
                metrics["duration_ms"] = metrics["total_ms"]
            if isinstance(receipt, dict):
                wave_id = receipt.get("wave_id")
                wave_generation = receipt.get("wave_generation")
                if isinstance(wave_id, str) and isinstance(wave_generation, int):
                    self.wave_generations[wave_id] = wave_generation
            return result

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.call("worker.quit", {})
            except StbError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=2)
        self.state = ContextState.CLOSED


class Supervisor:
    def __init__(
        self,
        settings: Settings | None = None,
        launcher: WorkerLauncher | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.launcher = launcher or build_launcher(self.settings.launcher)
        self.contexts: dict[str, WorkerChannel] = {}
        self._lock = threading.Lock()
        self._metric_lock = threading.Lock()
        self._metrics: dict[str, dict[str, float]] = defaultdict(
            lambda: {
                "count": 0,
                "complete": 0,
                "partial": 0,
                "failed": 0,
                "errors": 0,
                "total_ms": 0,
                "max_ms": 0,
                "queue_ms": 0,
                "npi_ms": 0,
                "python_ms": 0,
                "serialization_ms": 0,
                "transport_ms": 0,
                "input_bytes": 0,
                "response_bytes": 0,
                "scanned": 0,
                "returned": 0,
            }
        )
        self._history: deque[dict[str, Any]] = deque(maxlen=2000)
        self._trace_config: dict[str, Any] = {
            "enabled": False,
            "sample_rate": 0.0,
        }
        self._cursors = CursorRegistry()

    def _record(
        self,
        context_id: str,
        method: str,
        started: float,
        error_code: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = (time.perf_counter() - started) * 1000
        status = result.get("status") if isinstance(result, dict) else None
        receipt = result.get("receipt") if isinstance(result, dict) else {}
        metrics = receipt.get("metrics") if isinstance(receipt, dict) else {}
        limits = receipt.get("limits") if isinstance(receipt, dict) else {}
        request_id = (
            receipt.get("request_id")
            if isinstance(receipt, dict) and receipt.get("request_id")
            else f"sup-{uuid.uuid4().hex[:16]}"
        )
        with self._metric_lock:
            metric = self._metrics[method]
            metric["count"] += 1
            metric["errors"] += int(error_code is not None)
            metric["total_ms"] += duration_ms
            metric["max_ms"] = max(metric["max_ms"], duration_ms)
            if status in {"complete", "partial", "failed"}:
                metric[status] += 1
            for key in (
                "queue_ms",
                "npi_ms",
                "python_ms",
                "serialization_ms",
                "transport_ms",
                "input_bytes",
                "response_bytes",
            ):
                if isinstance(metrics, dict) and metrics.get(key) is not None:
                    metric[key] += float(metrics[key])
            for key in ("scanned", "returned"):
                if isinstance(limits, dict) and limits.get(key) is not None:
                    metric[key] += float(limits[key])
            self._history.append(
                {
                    "request_id": request_id,
                    "context_id": context_id,
                    "method": method,
                    "status": status or ("failed" if error_code else "complete"),
                    "duration_ms": duration_ms,
                    "error_code": error_code,
                    "metrics": metrics if isinstance(metrics, dict) else {},
                    "limits": limits if isinstance(limits, dict) else {},
                    "timestamp": time.time(),
                }
            )

    def open_context(
        self,
        context_id: str,
        backend: str | None = None,
        design_spec: dict[str, Any] | None = None,
        wave_specs: list[dict[str, Any]] | None = None,
        generation: int = 1,
    ) -> dict[str, Any]:
        backend_name = backend or self.settings.backend
        if backend_name == "verdi" and self.settings.verdi_home is None:
            raise StbError(
                "invalid_request",
                "STB_VERDI_HOME or VERDI_HOME is required for the Verdi backend",
            )
        with self._lock:
            if context_id in self.contexts and self.contexts[context_id].state != ContextState.CLOSED:
                raise StbError("invalid_request", f"context already exists: {context_id}")
            active = sum(c.state == ContextState.ACTIVE for c in self.contexts.values())
            if active >= self.settings.max_active_contexts:
                raise StbError(
                    "active_context_limit_reached",
                    "active context limit reached",
                    {"limit": self.settings.max_active_contexts},
                )
            log_root = self.settings.artifact_root.parent / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            log_path = log_root / f"{context_id}.log"
            command = [
                str(self.settings.worker_python),
                "-m",
                "stb.worker",
                "--context-id",
                context_id,
                "--backend",
                backend_name,
                "--log-path",
                str(log_path),
            ]
            process = self.launcher.launch(command, log_path)
            channel = WorkerChannel(
                context_id,
                process,
                threading.Lock(),
                generation=generation,
                log_path=log_path,
                backend_name=backend_name,
                design_spec=design_spec,
                wave_specs=list(wave_specs or []),
                hard_timeout_sec=self.settings.hard_timeout_sec,
            )
            self.contexts[context_id] = channel
        try:
            channel.call(
                "worker.open",
                {
                    "verdi_home": (
                        str(self.settings.verdi_home)
                        if self.settings.verdi_home is not None
                        else None
                    ),
                    "verdi_release": self.settings.verdi_release,
                    "design_spec": design_spec,
                    "wave_specs": wave_specs or [],
                    "allowed_roots": [
                        str(path) for path in self.settings.allowed_root_paths
                    ],
                    "max_object_handles": self.settings.max_object_handles,
                    "allow_unverified_verdi": self.settings.allow_unverified_verdi,
                    "normal_response_bytes": self.settings.normal_response_bytes,
                    "hard_response_bytes": self.settings.hard_response_bytes,
                    "default_timeout_sec": self.settings.default_timeout_sec,
                    "generation": generation,
                },
            )
            pong = channel.call("worker.ping", {})
        except Exception:
            channel.close()
            self.contexts.pop(context_id, None)
            raise
        channel.state = ContextState.ACTIVE
        return {
            "context_id": context_id,
            "state": channel.state.value,
            "worker_generation": generation,
            "pid": pong["pid"],
            "launcher": self.launcher.name,
        }

    def call(self, context_id: str, method: str, args: dict[str, Any]) -> dict[str, Any]:
        channel = self.contexts.get(context_id)
        if channel is None:
            raise StbError("context_not_found", f"context not found: {context_id}")
        if channel.state != ContextState.ACTIVE:
            raise StbError("context_not_active", f"context is {channel.state.value}")
        started = time.perf_counter()
        try:
            result = channel.call(method, args)
        except StbError as exc:
            self._record(context_id, method, started, exc.code)
            raise
        response_error = None
        if result.get("status") == "failed" and isinstance(result.get("error"), dict):
            response_error = result["error"].get("code")
        self._record(context_id, method, started, response_error, result)
        return result

    def close_context(self, context_id: str) -> dict[str, Any]:
        channel = self.contexts.get(context_id)
        if channel is None:
            raise StbError("context_not_found", f"context not found: {context_id}")
        channel.close()
        return {"context_id": context_id, "state": channel.state.value}

    def reload_context(self, context_id: str) -> dict[str, Any]:
        channel = self.contexts.get(context_id)
        if channel is None:
            raise StbError("context_not_found", f"context not found: {context_id}")
        backend = channel.backend_name
        design_spec = channel.design_spec
        wave_specs = channel.wave_specs
        generation = channel.generation + 1
        channel.close()
        self.contexts.pop(context_id, None)
        result = self.open_context(
            context_id,
            backend,
            design_spec=design_spec,
            wave_specs=wave_specs,
            generation=generation,
        )
        return result

    def list_contexts(self) -> list[dict[str, Any]]:
        return [
            {
                "context_id": context_id,
                "state": channel.state.value,
                "worker_generation": channel.generation,
                "pid": channel.process.pid,
                "launcher": self.launcher.name,
                "backend": channel.backend_name,
            }
            for context_id, channel in sorted(self.contexts.items())
        ]

    def close_all(self) -> None:
        for context_id in list(self.contexts):
            try:
                self.close_context(context_id)
            except Exception:
                pass
        self._cursors.clear()

    def _transition_time_fs(self, row: dict[str, Any]) -> int:
        point = row["time"]
        _, _, scale_fs = parse_scale_unit(point["raw_scale_unit"])
        return int(point["raw_ticks"]) * scale_fs

    def _cross_response(
        self,
        started: float,
        data: dict[str, Any],
        status: str = "complete",
    ) -> dict[str, Any]:
        limits = {
            "truncated": bool(data.get("truncated", False)),
            "termination_reason": data.get("termination_reason"),
            "scanned": data.get("scanned"),
            "returned": data.get("returned"),
            "next_cursor": data.get("next_cursor"),
        }
        return {
            "status": status,
            "data": data,
            "receipt": {
                "api_version": "stb.v1",
                "request_id": f"sup-{uuid.uuid4().hex[:16]}",
                "backend": "supervisor",
                "limits": {key: value for key, value in limits.items() if value is not None},
                "metrics": {
                    "duration_ms": (time.perf_counter() - started) * 1000,
                    "npi_calls": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                },
            },
        }

    def cross_wave_compute(self, args: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        operation = args["operation"]
        if operation not in {"compare", "first_divergence"}:
            raise StbError(
                "unsupported_operation",
                "cross-context wave_compute supports compare and first_divergence",
            )
        left = args["left"]
        right = args["right"]
        left_context = left["context_id"]
        right_context = right["context_id"]
        if left_context == right_context:
            raise StbError("invalid_request", "cross comparison requires two contexts")
        left_channel = self.contexts.get(left_context)
        right_channel = self.contexts.get(right_context)
        if left_channel is None or right_channel is None:
            missing = left_context if left_channel is None else right_context
            raise StbError("context_not_found", f"context not found: {missing}")
        if left["wave_id"] not in left_channel.wave_generations:
            self.call(
                left_context,
                "wave_manage",
                {"action": "status", "wave_id": left["wave_id"]},
            )
        if right["wave_id"] not in right_channel.wave_generations:
            self.call(
                right_context,
                "wave_manage",
                {"action": "status", "wave_id": right["wave_id"]},
            )
        if operation == "compare":
            times = args["times"]
            left_result = self.call(
                left_context,
                "wave_value",
                {
                    "wave_id": left["wave_id"],
                    "signals": [left["signal"]],
                    "times": times,
                },
            )
            right_result = self.call(
                right_context,
                "wave_value",
                {
                    "wave_id": right["wave_id"],
                    "signals": [right["signal"]],
                    "times": times,
                },
            )
            rows = []
            for time_spec, left_row, right_row in zip(
                times,
                left_result["data"]["values"],
                right_result["data"]["values"],
            ):
                equal = (
                    left_row.get("ok")
                    and right_row.get("ok")
                    and left_row["value"] == right_row["value"]
                )
                rows.append(
                    {
                        "time": time_spec,
                        "left": left_row,
                        "right": right_row,
                        "equal": bool(equal),
                    }
                )
            return self._cross_response(
                started,
                {
                    "operation": operation,
                    "context_mode": "cross",
                    "left_context_id": left_context,
                    "right_context_id": right_context,
                    "rows": rows,
                    "returned": len(rows),
                },
            )

        request_key = {
            "operation": operation,
            "left": left,
            "right": right,
            "start": args["start"],
            "end": args["end"],
            "max_transitions": args.get("max_transitions", 1_000_000),
            "left_worker_generation": left_channel.generation,
            "right_worker_generation": right_channel.generation,
            "left_wave_generation": left_channel.wave_generations.get(left["wave_id"]),
            "right_wave_generation": right_channel.wave_generations.get(right["wave_id"]),
        }
        state = self._cursors.get(args.get("cursor"))
        if state is not None and state["key"] != request_key:
            raise StbError("cursor_mismatch", "cursor does not match cross comparison")
        state = state or {
            "key": request_key,
            "left_cursor": None,
            "right_cursor": None,
            "left_buffer": [],
            "right_buffer": [],
            "processed_through_fs": int(parse_time(args["start"])),
        }
        maximum = int(args.get("max_transitions", 1_000_000))

        def fetch(
            context_id: str,
            side: dict[str, Any],
            cursor: str | None,
        ) -> tuple[list[dict[str, Any]], str | None]:
            response = self.call(
                context_id,
                "wave_changes",
                {
                    "wave_id": side["wave_id"],
                    "signals": [side["signal"]],
                    "start": args["start"],
                    "end": args["end"],
                    "direction": "forward",
                    "max_changes": maximum,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            item = response["data"]["signals"][0]
            if not item["ok"]:
                raise StbError(item["error_code"], f"signal unavailable: {side['signal']}")
            return item["changes"], response["data"].get("next_cursor")

        left_rows, left_cursor = fetch(
            left_context, left, state.get("left_cursor")
        )
        right_rows, right_cursor = fetch(
            right_context, right, state.get("right_cursor")
        )
        left_buffer = list(state.get("left_buffer") or []) + left_rows
        right_buffer = list(state.get("right_buffer") or []) + right_rows
        end_fs = int(parse_time(args["end"]))
        left_watermark = (
            self._transition_time_fs(left_buffer[-1]) if left_cursor else end_fs
        )
        right_watermark = (
            self._transition_time_fs(right_buffer[-1]) if right_cursor else end_fs
        )
        watermark = min(left_watermark, right_watermark)
        processed = int(state["processed_through_fs"])
        times_fs = sorted(
            {processed, watermark}
            | {
                self._transition_time_fs(row)
                for row in left_buffer + right_buffer
                if processed <= self._transition_time_fs(row) <= watermark
            }
        )
        time_specs = [f"{value}fs" for value in times_fs]
        left_values = self.call(
            left_context,
            "wave_value",
            {
                "wave_id": left["wave_id"],
                "signals": [left["signal"]],
                "times": time_specs,
            },
        )["data"]["values"]
        right_values = self.call(
            right_context,
            "wave_value",
            {
                "wave_id": right["wave_id"],
                "signals": [right["signal"]],
                "times": time_specs,
            },
        )["data"]["values"]
        for time_spec, left_row, right_row in zip(
            time_specs, left_values, right_values
        ):
            if left_row.get("ok") and right_row.get("ok") and left_row["value"] != right_row["value"]:
                return self._cross_response(
                    started,
                    {
                        "operation": operation,
                        "context_mode": "cross",
                        "left_context_id": left_context,
                        "right_context_id": right_context,
                        "divergence": {
                            "time": time_spec,
                            "left": left_row,
                            "right": right_row,
                        },
                        "scanned": len(times_fs),
                        "returned": 1,
                    },
                )
        left_buffer = [
            row for row in left_buffer if self._transition_time_fs(row) > watermark
        ]
        right_buffer = [
            row for row in right_buffer if self._transition_time_fs(row) > watermark
        ]
        truncated = bool(left_cursor or right_cursor or left_buffer or right_buffer)
        next_cursor = None
        if truncated:
            next_cursor = self._cursors.issue(
                {
                    "key": request_key,
                    "left_cursor": left_cursor,
                    "right_cursor": right_cursor,
                    "left_buffer": left_buffer,
                    "right_buffer": right_buffer,
                    "processed_through_fs": watermark,
                }
            )
        data = {
            "operation": operation,
            "context_mode": "cross",
            "left_context_id": left_context,
            "right_context_id": right_context,
            "divergence": None,
            "scanned": len(times_fs),
            "returned": 0,
            "truncated": truncated,
            "termination_reason": "transition_limit" if truncated else None,
            "next_cursor": next_cursor,
        }
        return self._cross_response(started, data, "partial" if truncated else "complete")

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metric_lock:
            operations = {}
            for name, metric in self._metrics.items():
                count = int(metric["count"])
                operations[name] = {
                    **metric,
                    "mean_ms": metric["total_ms"] / count if count else 0,
                    "mean_queue_ms": metric["queue_ms"] / count if count else 0,
                    "mean_npi_ms": metric["npi_ms"] / count if count else 0,
                    "mean_python_ms": metric["python_ms"] / count if count else 0,
                    "mean_serialization_ms": metric["serialization_ms"] / count
                    if count
                    else 0,
                    "mean_transport_ms": metric["transport_ms"] / count if count else 0,
                }
            return {"operations": operations, "history_size": len(self._history)}

    def metrics_reset(self) -> dict[str, Any]:
        with self._metric_lock:
            self._metrics.clear()
            self._history.clear()
        return {"reset": True}

    def configure_trace(self, config: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(config.get("enabled", False))
        sample_rate = float(config.get("sample_rate", 1.0 if enabled else 0.0))
        if not 0.0 <= sample_rate <= 1.0:
            raise StbError("invalid_request", "sample_rate must be between 0 and 1")
        self._trace_config = {"enabled": enabled, "sample_rate": sample_rate}
        return dict(self._trace_config)

    def trace_config(self) -> dict[str, Any]:
        return dict(self._trace_config)

    def request_history(
        self,
        limit: int = 100,
        context_id: str | None = None,
        method: str | None = None,
        error_code: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._metric_lock:
            rows = list(self._history)
            if context_id:
                rows = [row for row in rows if row["context_id"] == context_id]
            if method:
                rows = [row for row in rows if row["method"] == method]
            if error_code:
                rows = [row for row in rows if row["error_code"] == error_code]
            return rows[-max(1, min(limit, 2000)) :]

    def read_logs(
        self, context_id: str, max_lines: int = 200, contains: str | None = None
    ) -> dict[str, Any]:
        channel = self.contexts.get(context_id)
        if channel is None:
            raise StbError("context_not_found", f"context not found: {context_id}")
        if channel.log_path is None or not channel.log_path.exists():
            return {"context_id": context_id, "lines": []}
        lines = channel.log_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        if contains:
            lines = [line for line in lines if contains in line]
        return {
            "context_id": context_id,
            "lines": lines[-max(1, min(max_lines, 2000)) :],
            "truncated": len(lines) > max_lines,
        }
