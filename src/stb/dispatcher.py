from __future__ import annotations

import importlib.util
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import threading
from typing import Any

import psutil

from stb import API_VERSION, __version__
from stb.artifacts import ArtifactManager
from stb.config import Settings
from stb.errors import StbError
from stb.supervisor import Supervisor
from stb.verdi_compat import (
    REQUIRED_NPI_SYMBOLS,
    VERIFIED_VERDI_RELEASES,
    detect_verdi_release,
    is_verified_release,
)
from stb.schemas import validate_public_request
from stb.tool_inventory import CORE_TOOLS, DEV_TOOLS
from stb.response_limits import enforce_response_limit


class ToolDispatcher:
    """Shared MCP, CLI, and test entry point for all public STB tools."""

    def __init__(
        self,
        settings: Settings,
        supervisor: Supervisor,
        artifacts: ArtifactManager,
    ) -> None:
        self.settings = settings
        self.supervisor = supervisor
        self.artifacts = artifacts

    def _failure(self, exc: StbError) -> dict[str, Any]:
        return {"status": "failed", "error": exc.as_dict()}

    def _worker(
        self, context_id: str, method: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.supervisor.call(context_id, method, request)

    def dispatch(self, tool: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        if tool not in CORE_TOOLS and tool not in DEV_TOOLS:
            return self._failure(
                StbError("unsupported_operation", f"unknown public tool: {tool}")
            )
        if tool in DEV_TOOLS and not self.settings.dev_tools:
            return self._failure(
                StbError("unsupported_capability", "development tools are disabled")
            )
        try:
            request = validate_public_request(tool, request)
            handler = getattr(self, f"_tool_{tool}")
            result = handler(request)
            enforce_response_limit(
                result,
                self.settings.normal_response_bytes,
                self.settings.hard_response_bytes,
            )
            return result
        except KeyError as exc:
            return self._failure(
                StbError("invalid_request", f"missing field: {exc.args[0]}")
            )
        except StbError as exc:
            return self._failure(exc)

    def _tool_context_manage(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request["action"]
        context_id = request.get("context_id")
        if action == "open":
            if not context_id:
                raise StbError("invalid_request", "context_id is required")
            return self.supervisor.open_context(
                context_id,
                request.get("backend"),
                design_spec=request.get("design_spec"),
                wave_specs=request.get("wave_specs"),
            )
        if action == "reload":
            if not context_id:
                raise StbError("invalid_request", "context_id is required")
            return self.supervisor.reload_context(context_id)
        if action == "close":
            if not context_id:
                raise StbError("invalid_request", "context_id is required")
            return self.supervisor.close_context(context_id)
        if action == "list":
            return {"contexts": self.supervisor.list_contexts()}
        if action == "status":
            contexts = {
                item["context_id"]: item for item in self.supervisor.list_contexts()
            }
            if not context_id or context_id not in contexts:
                raise StbError("context_not_found", f"context not found: {context_id}")
            return contexts[context_id]
        if action == "release_objects":
            if not context_id:
                raise StbError("invalid_request", "context_id is required")
            return self._worker(
                context_id,
                "release_objects",
                {"object_ids": request.get("object_ids") or []},
            )
        raise StbError("unsupported_operation", f"unsupported context action: {action}")

    def _tool_wave_manage(self, request: dict[str, Any]) -> dict[str, Any]:
        context_id = request["context_id"]
        return self._worker(
            context_id,
            "wave_manage",
            {
                "action": request["action"],
                "wave_id": request.get("wave_id"),
                "path": request.get("path"),
            },
        )

    def _tool_catalog(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker(
            request["context_id"],
            "catalog",
            {
                "kind": request["kind"],
                "filters": request.get("filters") or {},
            },
        )

    def _worker_passthrough(
        self, tool: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        payload = request.get("request")
        if payload is None:
            payload = {
                key: value
                for key, value in request.items()
                if key != "context_id"
            }
        return self._worker(request["context_id"], tool, payload)

    def _tool_object_resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("object_resolve", request)

    def _tool_object_get(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("object_get", request)

    def _tool_object_query(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("object_query", request)

    def _tool_object_traverse(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("object_traverse", request)

    def _tool_connectivity_direct(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("connectivity_direct", request)

    def _tool_trace(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("trace", request)

    def _tool_trace_active_driver(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("trace_active_driver", request)

    def _tool_trace_value_origin(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("trace_value_origin", request)

    def _tool_wave_value(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("wave_value", request)

    def _tool_wave_changes(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("wave_changes", request)

    def _tool_wave_compute(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("request") or {}
        left = payload.get("left") or {}
        right = payload.get("right") or {}
        if (
            payload.get("context_mode") == "cross"
            or (
                left.get("context_id")
                and right.get("context_id")
                and left.get("context_id") != right.get("context_id")
            )
        ):
            return self.supervisor.cross_wave_compute(payload)
        return self._worker_passthrough("wave_compute", request)

    def _tool_source_context(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("source_context", request)

    def _tool_assertion_structure(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("assertion_structure", request)

    def _tool_mapping(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._worker_passthrough("mapping", request)

    def _tool_artifact(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request["action"]
        payload = request.get("request") or {}
        if action == "export":
            context_id = payload["context_id"]
            method = payload["method"]
            args = payload.get("args") or {}
            if method not in CORE_TOOLS or method in {"context_manage", "artifact"}:
                raise StbError(
                    "unsupported_operation",
                    f"artifact cannot execute operation: {method}",
                )
            return self.artifacts.submit(
                lambda cancel_event: self._export_core_operation(
                    context_id, method, args, cancel_event
                ),
                {
                    "kind": "core_operation",
                    "context_id": context_id,
                    "method": method,
                    "args": args,
                },
            )
        if action == "status":
            return self.artifacts.status(payload["job_id"])
        if action == "list":
            return {"jobs": self.artifacts.list()}
        if action == "cancel":
            return self.artifacts.cancel(payload["job_id"])
        if action == "delete":
            return self.artifacts.delete(payload["artifact_id"])
        raise StbError("unsupported_operation", f"unsupported artifact action: {action}")

    def _execute_core_operation(
        self, context_id: str, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "wave_compute":
            left = args.get("left") or {}
            right = args.get("right") or {}
            if (
                args.get("context_mode") == "cross"
                or (
                    left.get("context_id")
                    and right.get("context_id")
                    and left.get("context_id") != right.get("context_id")
                )
            ):
                return self.supervisor.cross_wave_compute(args)
        return self.supervisor.call(context_id, method, args)

    def _export_core_operation(
        self,
        context_id: str,
        method: str,
        args: dict[str, Any],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        chunks = []
        request_args = dict(args)
        for chunk_index in range(10_000):
            if cancel_event.is_set():
                raise StbError("job_cancelled", "artifact export cancelled")
            response = self._execute_core_operation(context_id, method, request_args)
            chunks.append({"chunk_index": chunk_index, "response": response})
            data = response.get("data") or {}
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                next_cursor = (
                    (response.get("receipt") or {}).get("limits") or {}
                ).get("next_cursor")
            if not next_cursor:
                return {
                    "schema_version": "stb.operation-artifact.v1",
                    "request": {
                        "context_id": context_id,
                        "method": method,
                        "args": args,
                    },
                    "chunks": chunks,
                    "summary": {
                        "chunk_count": len(chunks),
                        "final_status": response.get("status", "complete"),
                        "complete": True,
                    },
                }
            request_args = {**request_args, "cursor": next_cursor}
            time.sleep(0)
        raise StbError(
            "limit_exceeded",
            "artifact export exceeded 10000 continuation chunks",
        )

    def _tool_admin_doctor(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.settings.verdi_home is None:
            npi_path = None
            detected_release = self.settings.verdi_release
            worker_probe = {
                "pynpi_importable": False,
                "missing_symbols": [],
                "unexpected_module_origins": {},
                "worker_stb_importable": False,
                "error": "STB_VERDI_HOME or VERDI_HOME is not set",
            }
        else:
            npi_path = self.settings.verdi_home / "share/NPI/python"
            detected_release = detect_verdi_release(
                self.settings.verdi_home,
                override=self.settings.verdi_release,
            )
            worker_probe = self._probe_worker_environment(npi_path)
        return {
            "status": "complete",
            "api_version": API_VERSION,
            "stb_version": __version__,
            "python": sys.version,
            "pid": os.getpid(),
            "verdi_home": (
                str(self.settings.verdi_home)
                if self.settings.verdi_home is not None
                else None
            ),
            "verdi_version": detected_release,
            "verified_verdi": (
                is_verified_release(detected_release)
                if detected_release is not None
                else False
            ),
            "verified_releases": sorted(VERIFIED_VERDI_RELEASES),
            "worker_python": str(self.settings.worker_python),
            "worker_python_exists": (
                self.settings.worker_python.is_file()
                or shutil.which(str(self.settings.worker_python)) is not None
            ),
            "pynpi_path_exists": npi_path.is_dir() if npi_path is not None else False,
            "pynpi_importable": worker_probe["pynpi_importable"],
            "pynpi_missing_symbols": worker_probe["missing_symbols"],
            "pynpi_unexpected_module_origins": worker_probe[
                "unexpected_module_origins"
            ],
            "worker_stb_importable": worker_probe["worker_stb_importable"],
            "worker_probe_error": worker_probe["error"],
            "server_pynpi_importable": importlib.util.find_spec("pynpi") is not None,
            "active_contexts": self.supervisor.list_contexts(),
        }

    def _probe_worker_environment(self, npi_path: Any) -> dict[str, Any]:
        result = {
            "pynpi_importable": False,
            "missing_symbols": [],
            "unexpected_module_origins": {},
            "worker_stb_importable": False,
            "error": None,
        }
        script = """
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
root = Path(sys.argv[1]).resolve()
required = json.loads(sys.argv[2])
result = {
    "pynpi_importable": False,
    "missing_symbols": [],
    "unexpected_module_origins": {},
    "worker_stb_importable": importlib.util.find_spec("stb") is not None,
    "error": None,
}
try:
    from pynpi import lang, netlist, npisys, text, waveform
    modules = {
        "lang": lang,
        "netlist": netlist,
        "npisys": npisys,
        "text": text,
        "waveform": waveform,
    }
    result["pynpi_importable"] = True
    for module_name, module in modules.items():
        origin = getattr(module, "__file__", None)
        if not origin:
            result["unexpected_module_origins"][module_name] = "<unknown>"
            continue
        resolved = Path(origin).resolve()
        if resolved != root and root not in resolved.parents:
            result["unexpected_module_origins"][module_name] = str(resolved)
    result["missing_symbols"] = [
        f"{module_name}.{symbol}"
        for module_name, symbols in required.items()
        for symbol in symbols
        if not hasattr(modules[module_name], symbol)
    ]
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result, sort_keys=True))
"""
        env = os.environ.copy()
        env["VERDI_HOME"] = str(self.settings.verdi_home)
        try:
            completed = subprocess.run(
                [
                    str(self.settings.worker_python),
                    "-c",
                    script,
                    str(npi_path),
                    json.dumps(REQUIRED_NPI_SYMBOLS, sort_keys=True),
                ],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if lines:
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError:
                pass
        detail = completed.stderr.strip() or completed.stdout.strip()
        result["error"] = detail[-2000:] or f"worker probe exited {completed.returncode}"
        return result

    def _tool_admin_metrics(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action", "snapshot")
        payload = request.get("request") or {}
        if action == "snapshot":
            snapshot = self.supervisor.metrics_snapshot()
            method = payload.get("method")
            if method:
                snapshot["operations"] = {
                    name: value
                    for name, value in snapshot["operations"].items()
                    if name == method
                }
            return {
                "status": "complete",
                "timestamp": time.time(),
                "metrics": snapshot,
                "contexts": self.supervisor.list_contexts(),
            }
        if action == "reset":
            return {"status": "complete", **self.supervisor.metrics_reset()}
        if action == "export":
            return {
                "status": "complete",
                **self.artifacts.submit(
                    lambda: {
                        "schema_version": "stb.metrics.v1",
                        "timestamp": time.time(),
                        "metrics": self.supervisor.metrics_snapshot(),
                        "contexts": self.supervisor.list_contexts(),
                    }
                ),
            }
        if action == "compare":
            baseline = payload.get("baseline") or {}
            current = self.supervisor.metrics_snapshot()
            comparisons = {}
            for name, metric in current["operations"].items():
                old = (baseline.get("operations") or {}).get(name, {})
                comparisons[name] = {
                    "current_mean_ms": metric["mean_ms"],
                    "baseline_mean_ms": old.get("mean_ms"),
                    "delta_mean_ms": (
                        metric["mean_ms"] - float(old["mean_ms"])
                        if old.get("mean_ms") is not None
                        else None
                    ),
                }
            return {"status": "complete", "comparisons": comparisons}
        raise StbError("unsupported_operation", f"admin_metrics.{action}")

    def _tool_admin_trace(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request["action"]
        payload = request.get("request") or {}
        if action == "configure":
            return {
                "status": "complete",
                "configuration": self.supervisor.configure_trace(payload),
            }
        if action in {"get", "list"}:
            return {
                "status": "complete",
                "configuration": self.supervisor.trace_config(),
                "requests": self.supervisor.request_history(
                    int(payload.get("limit", 100)),
                    payload.get("context_id"),
                    payload.get("method"),
                    payload.get("error_code"),
                ),
            }
        if action == "export":
            return {
                "status": "complete",
                **self.artifacts.submit(
                    lambda: {
                        "schema_version": "stb.trace.v1",
                        "configuration": self.supervisor.trace_config(),
                        "requests": self.supervisor.request_history(
                            int(payload.get("limit", 2000)),
                            payload.get("context_id"),
                            payload.get("method"),
                            payload.get("error_code"),
                        ),
                    }
                ),
            }
        raise StbError("unsupported_operation", f"admin_trace.{action}")

    def _tool_admin_logs(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        payload = request.get("request") or {}
        if action not in {"list", "get"}:
            raise StbError("unsupported_operation", f"admin_logs.{action}")
        return {
            "status": "complete",
            **self.supervisor.read_logs(
                payload["context_id"],
                int(payload.get("max_lines", 200)),
                payload.get("contains"),
            ),
        }

    def _tool_admin_benchmark(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("request") or request
        context_id = payload["context_id"]
        method = payload["method"]
        args = payload.get("args") or {}
        iterations = max(1, min(int(payload.get("iterations", 5)), 100))
        durations = []
        response_bytes = []
        input_bytes = []
        npi_calls = []
        npi_ms = []
        python_ms = []
        serialization_ms = []
        transport_ms = []
        scanned = []
        returned = []
        statuses: dict[str, int] = {}
        last_metrics: dict[str, Any] = {}
        last_limits: dict[str, Any] = {}
        worker_pid = None
        for item in self.supervisor.list_contexts():
            if item["context_id"] == context_id:
                worker_pid = item["pid"]
                break
        process = psutil.Process(os.getpid())
        worker_process = psutil.Process(worker_pid) if worker_pid else None
        for _ in range(iterations):
            started = time.perf_counter()
            result = self.supervisor.call(context_id, method, args)
            durations.append((time.perf_counter() - started) * 1000)
            statuses[result.get("status", "complete")] = (
                statuses.get(result.get("status", "complete"), 0) + 1
            )
            receipt = result.get("receipt") or {}
            last_metrics = receipt.get("metrics") or {}
            last_limits = receipt.get("limits") or {}
            for source, sink, key in (
                (last_metrics, input_bytes, "input_bytes"),
                (last_metrics, response_bytes, "response_bytes"),
                (last_metrics, npi_calls, "npi_calls"),
                (last_metrics, npi_ms, "npi_ms"),
                (last_metrics, python_ms, "python_ms"),
                (last_metrics, serialization_ms, "serialization_ms"),
                (last_metrics, transport_ms, "transport_ms"),
                (last_limits, scanned, "scanned"),
                (last_limits, returned, "returned"),
            ):
                if source.get(key) is not None:
                    sink.append(float(source[key]))
        durations.sort()
        median = statistics.median(durations)
        p95 = durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)]
        baseline = payload.get("baseline") or {}
        rss = process.memory_info().rss
        worker_rss = worker_process.memory_info().rss if worker_process else None
        return {
            "status": "complete",
            "benchmark_version": "stb.bench.v1",
            "case": {
                "context_id": context_id,
                "method": method,
                "iterations": iterations,
                "statuses": statuses,
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "stb_version": __version__,
                "launcher": self.settings.launcher,
                "backend": self.settings.backend,
            },
            "request_bytes": len(json.dumps(args, separators=(",", ":")).encode()),
            "median_ms": median,
            "p95_ms": p95,
            "min_ms": durations[0],
            "max_ms": durations[-1],
            "mean_ms": statistics.fmean(durations),
            "rss_bytes": rss,
            "worker_rss_bytes": worker_rss,
            "cpu_times": process.cpu_times()._asdict(),
            "metrics": {
                "input_bytes": int(max(input_bytes)) if input_bytes else 0,
                "response_bytes": int(max(response_bytes)) if response_bytes else 0,
                "npi_calls": int(sum(npi_calls)),
                "npi_ms": sum(npi_ms),
                "python_ms": sum(python_ms),
                "serialization_ms": sum(serialization_ms),
                "transport_ms": sum(transport_ms),
                "scanned": int(sum(scanned)),
                "returned": int(sum(returned)),
                "last_receipt_metrics": last_metrics,
                "last_receipt_limits": last_limits,
            },
            "baseline_delta_ms": (
                median - float(baseline["median_ms"])
                if baseline.get("median_ms") is not None
                else None
            ),
        }

    def _tool_admin_selftest(self, request: dict[str, Any]) -> dict[str, Any]:
        context_id = f"selftest-{os.getpid()}"
        checks = []
        try:
            opened = self.supervisor.open_context(context_id, backend="fake")
            checks.append({"name": "worker_open", "status": "pass", "data": opened})
            result = self.supervisor.call(
                context_id, "object_resolve", {"name": "top.u_core.req"}
            )
            checks.append(
                {
                    "name": "fake_object_resolve",
                    "status": (
                        "pass" if result.get("status") == "complete" else "fail"
                    ),
                }
            )
        except Exception as exc:
            checks.append(
                {"name": "fake_transport", "status": "fail", "error": str(exc)}
            )
        finally:
            active = {
                item["context_id"] for item in self.supervisor.list_contexts()
            }
            if context_id in active:
                self.supervisor.close_context(context_id)
        return {
            "status": (
                "complete"
                if all(item["status"] == "pass" for item in checks)
                else "failed"
            ),
            "checks": checks,
        }
