from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from typing import Any

from stb.backends.fake import FakeBackend
from stb.backends.verdi import VerdiBackend
from stb.errors import StbError
from stb.protocol import read_message, write_message
from stb.service import StbService


def _dispatch(service: StbService, method: str, args: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "catalog": lambda: service.catalog(args["kind"], args.get("filters")),
        "object_resolve": lambda: service.object_resolve(args),
        "object_get": lambda: service.object_get(args),
        "object_query": lambda: service.object_query(args),
        "object_traverse": lambda: service.object_traverse(args),
        "release_objects": lambda: service._call(
            "release_objects",
            lambda: service.backend.release_objects(args.get("object_ids") or []),
            args,
        ),
        "wave_manage": lambda: service._call(
            "wave_manage", lambda: service.backend.wave_manage(args), args
        ),
        "wave_value": lambda: service._call(
            "wave_value", lambda: service.backend.wave_value(args), args
        ),
        "wave_changes": lambda: service._call(
            "wave_changes", lambda: service.backend.wave_changes(args), args
        ),
        "connectivity_direct": lambda: service._call(
            "connectivity_direct", lambda: service.backend.connectivity_direct(args), args
        ),
        "trace": lambda: service._call("trace", lambda: service.backend.trace(args), args),
        "trace_active_driver": lambda: service._call(
            "trace_active_driver",
            lambda: service.backend.trace_active_driver(args),
            args,
        ),
        "trace_value_origin": lambda: service._call(
            "trace_value_origin",
            lambda: service.backend.trace_value_origin(args),
            args,
        ),
        "wave_compute": lambda: service._call(
            "wave_compute", lambda: service.backend.wave_compute(args), args
        ),
        "source_context": lambda: service._call(
            "source_context", lambda: service.backend.source_context(args), args
        ),
        "assertion_structure": lambda: service._call(
            "assertion_structure",
            lambda: service.backend.assertion_structure(args),
            args,
        ),
        "mapping": lambda: service._call(
            "mapping", lambda: service.backend.mapping(args), args
        ),
    }
    if method == "worker.ping":
        return {"ok": True, "pid": os.getpid()}
    if method not in handlers:
        return {
            "status": "failed",
            "error": {
                "code": "unsupported_operation",
                "message": f"unsupported worker method: {method}",
                "recoverable": True,
            },
        }
    return handlers[method]()


def run(context_id: str, backend_name: str, log_path: str | None = None) -> int:
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    log = open(log_path, "a", buffering=1) if log_path else tempfile.TemporaryFile(mode="w+")
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())

    def log_event(event: dict[str, Any]) -> None:
        event = {
            "schema_version": "stb.worker-log.v1",
            "timestamp": time.time(),
            "context_id": context_id,
            "backend": backend_name,
            **event,
        }
        print(json.dumps(event, separators=(",", ":")), file=sys.stderr, flush=True)

    service: StbService | None = None
    while request := read_message(sys.stdin):
        request_id = request.get("id")
        method = request.get("method", "")
        started = time.perf_counter()
        if method == "worker.quit":
            if service:
                service.backend.close()
            log_event(
                {
                    "event": "request",
                    "request_id": request_id,
                    "method": method,
                    "status": "complete",
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
            )
            write_message(protocol_out, {"id": request_id, "ok": True})
            return 0
        try:
            if method == "worker.open":
                args = request.get("args") or {}
                if backend_name == "fake":
                    backend = FakeBackend(
                        context_id,
                        generation=int(args.get("generation", 1)),
                        wave_specs=args.get("wave_specs"),
                    )
                elif backend_name == "verdi":
                    backend = VerdiBackend(
                        context_id=context_id,
                        verdi_home=args["verdi_home"],
                        verdi_release=args.get("verdi_release"),
                        generation=int(args.get("generation", 1)),
                        design_spec=args.get("design_spec"),
                        wave_specs=args.get("wave_specs"),
                        allowed_roots=args.get("allowed_roots"),
                        max_object_handles=int(
                            args.get("max_object_handles", 100_000)
                        ),
                        allow_unverified_verdi=bool(
                            args.get("allow_unverified_verdi", False)
                        ),
                    )
                else:
                    raise RuntimeError(f"unknown backend: {backend_name}")
                service = StbService(
                    backend,
                    context_id,
                    normal_response_bytes=int(
                        args.get("normal_response_bytes", 4 << 20)
                    ),
                    hard_response_bytes=int(
                        args.get("hard_response_bytes", 16 << 20)
                    ),
                    default_timeout_sec=float(
                        args.get("default_timeout_sec", 120.0)
                    ),
                )
                write_message(
                    protocol_out,
                    {"id": request_id, "ok": True, "result": {"opened": True}},
                )
                log_event(
                    {
                        "event": "request",
                        "request_id": request_id,
                        "method": method,
                        "status": "complete",
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    }
                )
                continue
            if service is None:
                raise RuntimeError("worker is not opened")
            service.request_id_override = str(request_id)
            result = _dispatch(service, method, request.get("args") or {})
            service.request_id_override = None
            receipt = result.get("receipt") if isinstance(result, dict) else {}
            metrics = receipt.get("metrics") if isinstance(receipt, dict) else {}
            error = result.get("error") if isinstance(result, dict) else None
            log_event(
                {
                    "event": "request",
                    "request_id": receipt.get("request_id", request_id)
                    if isinstance(receipt, dict)
                    else request_id,
                    "protocol_request_id": request_id,
                    "method": method,
                    "status": result.get("status")
                    if isinstance(result, dict)
                    else "complete",
                    "error_code": error.get("code")
                    if isinstance(error, dict)
                    else None,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                    "input_bytes": metrics.get("input_bytes")
                    if isinstance(metrics, dict)
                    else None,
                    "response_bytes": metrics.get("response_bytes")
                    if isinstance(metrics, dict)
                    else None,
                    "npi_calls": metrics.get("npi_calls")
                    if isinstance(metrics, dict)
                    else None,
                }
            )
            write_message(protocol_out, {"id": request_id, "ok": True, "result": result})
        except Exception as exc:  # Worker boundary converts unexpected faults.
            if service is not None:
                service.request_id_override = None
            if isinstance(exc, StbError):
                error = exc.as_dict()
            else:
                traceback.print_exc(file=sys.stderr)
                error = {
                    "code": "worker_internal_error",
                    "message": str(exc),
                    "recoverable": False,
                }
            write_message(
                protocol_out,
                {
                    "id": request_id,
                    "ok": False,
                    "error": error,
                },
            )
            log_event(
                {
                    "event": "request",
                    "request_id": request_id,
                    "method": method,
                    "status": "failed",
                    "error_code": error.get("code"),
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
            )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-id", required=True)
    parser.add_argument("--backend", default="fake")
    parser.add_argument("--log-path")
    args = parser.parse_args()
    raise SystemExit(run(args.context_id, args.backend, args.log_path))


if __name__ == "__main__":
    main()
